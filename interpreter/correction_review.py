"""
Response Quality Feedback Loop chunk B (2026-09-10) — score the (bot draft,
human's actual final reply) pairs already collected by `api/worker.py::
_check_resolution` (`runs.human_action`/`human_reply`, migration `014`) and
surfaced but not judged by chunk A's `GET /api/feedback/corrections`.

`judge_correction()` classifies WHY a human changed a draft before sending
it — tone, a factual correction, a policy/pricing correction, a brevity
edit, or no meaningful difference (a typo fix) — via a single Groq-routed
`interpreter/llm.py` call. This is the same judge-routing approach
`interpreter/review.py`'s KIL contradiction judge and `eval/deepeval/
groq_model.py`'s `GroqJudge` both use (Groq via `llm.complete`, structured
JSON, robust parsing) — deliberately *not* the `deepeval` package itself,
since deepeval is a dev-only tool (`requirements-dev.txt`, not in the
runtime image) and this needs to run in the production worker via a sweep
(`interpreter/sweeps.py::correction_review_sweep`), not a hand-run script.

Chunk D (2026-09-10) adds `select_exemplars()`/`format_exemplars_block()`:
the first piece of this loop that actually changes what the bot says. It
turns judged corrections back into few-shot guidance for the `draft` node
via a new `correction_exemplars` node (`interpreter/registry.py`) —
opt-in, placed upstream of `draft` in a flow, so an existing flow's
behavior is unchanged until someone adds it. Deliberately excludes
`category in (none, unknown)` (no real lesson) and anything below
`min_severity` or older than `max_age_days` — an old correction can
reflect a policy that has since changed again, and re-surfacing it as
guidance would actively reintroduce the staleness this whole loop exists
to fix.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("interpreter.correction_review")

CATEGORIES = ("tone", "factual", "policy", "brevity", "none", "other")

_SYSTEM = (
    "You compare a customer-support bot's draft reply against what a human "
    "agent actually sent to the customer instead. Classify the MAIN reason "
    "the human changed it, and how much the change mattered.\n"
    "Categories:\n"
    "tone — same substance, different voice/formality/wording.\n"
    "factual — corrected or added a fact the draft got wrong, missed, or "
    "left vague.\n"
    "policy — corrected a policy, pricing, refund, or compliance statement.\n"
    "brevity — trimmed or expanded length/structure, same substance.\n"
    "none — no meaningful difference (e.g. a typo fix, near-identical "
    "wording).\n"
    "other — none of the above fit.\n"
    'Reply with ONLY a JSON object: {"category": <one of the above>, '
    '"severity": <0.0-1.0, how much the meaning or outcome for the '
    'customer changed>, "summary": "<one specific sentence>"}.'
)


def judge_correction(draft: str, human_reply: str, *, subject: str = "",
                     tenant_id: "str | None" = None) -> dict:
    """Best-effort — never raises. Returns `{"category", "severity",
    "summary"}`. `category="unknown"` on any failure (missing input, judge
    unavailable, malformed response) — kept distinct from the real verdict
    `category="none"` so a caller can tell "judged, nothing notable" apart
    from "couldn't judge it" and decide whether to retry."""
    from interpreter import llm

    if not (draft or "").strip() or not (human_reply or "").strip():
        return {"category": "unknown", "severity": None, "summary": "missing draft or reply"}

    user = (
        f"Customer's message subject: {subject or '(none)'}\n\n"
        f"# Bot's draft\n{draft}\n\n# What the human actually sent\n{human_reply}"
    )
    try:
        raw = llm.complete(system=_SYSTEM, user=user, model=llm.FAST_MODEL,
                           json_object=True, max_tokens=250, tenant_id=tenant_id)
        parsed = json.loads(llm._coerce_json(raw) or raw)
        category = parsed.get("category")
        if category not in CATEGORIES:
            category = "other"
        severity = parsed.get("severity")
        try:
            severity = round(max(0.0, min(1.0, float(severity))), 2)
        except (TypeError, ValueError):
            severity = None
        return {"category": category, "severity": severity,
                "summary": str(parsed.get("summary") or "")[:300]}
    except Exception as e:  # noqa: BLE001 — a judge failure must not break the sweep
        log.warning("judge_correction failed: %s", e)
        return {"category": "unknown", "severity": None, "summary": ""}


# ── exemplar selection for chunk D (few-shot guidance for `draft`) ──────
_LESSON_CATEGORIES = tuple(c for c in CATEGORIES if c not in ("none", "unknown"))


def select_exemplars(sb, tenant_id: "str | None", *, k: int = 2, pool: int = 50,
                     min_severity: float = 0.4, max_age_days: int = 90,
                     categories: "tuple[str, ...] | None" = None) -> list[dict]:
    """The `k` most severe, real judged corrections for this tenant, to show
    `draft` as few-shot "don't repeat this" guidance. Scoped to `tenant_id`
    (never cross-tenant), capped to the last `max_age_days` (an old
    correction may reflect a policy that has since changed again — reusing
    it as guidance could reintroduce the very staleness this loop exists to
    fix), and restricted to categories that carry a real lesson (excludes
    `none`/`unknown` by default). Best-effort — a query failure returns `[]`,
    never raises."""
    if not tenant_id:
        return []
    allowed = set(categories) if categories else set(_LESSON_CATEGORIES)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    try:
        rows = (sb.table("runs")
                .select("run_id, subject, draft, human_reply, correction_analysis, created_at")
                .eq("tenant_id", tenant_id)
                .not_.is_("correction_analysis", "null")
                .gte("created_at", cutoff)
                .order("created_at", desc=True).limit(pool).execute().data or [])
    except Exception as e:  # noqa: BLE001
        log.warning("select_exemplars query failed: %s", e)
        return []

    candidates = []
    for r in rows:
        ca = r.get("correction_analysis") or {}
        if ca.get("category") not in allowed:
            continue
        severity = ca.get("severity")
        if severity is None or severity < min_severity:
            continue
        if not (r.get("draft") or "").strip() or not (r.get("human_reply") or "").strip():
            continue
        candidates.append((severity, r))
    candidates.sort(key=lambda t: t[0], reverse=True)
    return [r for _, r in candidates[:max(k, 0)]]


def format_exemplars_block(exemplars: list[dict]) -> str:
    """Render `select_exemplars()`'s rows as a prompt block. Empty string
    when there's nothing to show, so a caller can unconditionally append
    the result without an extra `if`."""
    if not exemplars:
        return ""
    blocks = []
    for i, r in enumerate(exemplars, 1):
        ca = r.get("correction_analysis") or {}
        draft = (r.get("draft") or "").strip()[:300]
        reply = (r.get("human_reply") or "").strip()[:300]
        summary = ca.get("summary") or "(no summary)"
        blocks.append(
            f"{i}. [{ca.get('category')}, severity {ca.get('severity')}] "
            f"A past AI draft said: \"{draft}\"\n"
            f"A human corrected it to: \"{reply}\"\n"
            f"Why: {summary}"
        )
    return "\n\n".join(blocks)
