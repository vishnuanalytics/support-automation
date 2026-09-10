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
"""

from __future__ import annotations

import json
import logging

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
