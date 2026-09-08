"""
Intake checklists — turn a per-issue-type spec (`intake_checklists` table,
migration 101) into the *specific* things the `clarify` node should ask when a
customer's report is too thin to act on.

Flow, all best-effort (any failure -> "we learned nothing extra, ask nothing
extra", never raises):

    checklist = checklist_for(state)          # match by module / submodule /
                                              # case_type / keywords; most
                                              # specific wins, `priority` breaks ties
    if checklist:
        ex   = extract(checklist, state)      # detect rules -> known SF fields
                                              # -> one text LLM pass -> one
                                              # vision pass for `vision:true` gaps
        miss = gaps(checklist, ex["known"])   # required signals still unknown
        qs   = questions_for(miss, limit)     # the questions to actually ask
        fw   = field_writes(checklist, ex["known"])   # {SF field: value} to persist

`detect` rule shapes (any one key):
    {"any_of": ["swiggy", "zomato"]}     substring hit in subject+body+topic
    {"regex": "\\b[45]\\d\\d\\b"}         regex hit in the same text
    {"attachment_type": "image"}          an image (or "video") attachment is present
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from interpreter import llm

log = logging.getLogger("interpreter.intake")

_MODEL = "openai/gpt-oss-20b"          # classify-tier; cheap, JSON out
_MAX_QUESTIONS = 3
_VALUE_MAX = 300


# --------------------------------------------------------------------------- #
# loading + matching
# --------------------------------------------------------------------------- #
def load_checklists(tenant_id: str | None, sb=None) -> list[dict]:
    """Enabled checklist rows for the tenant. `[]` on any problem."""
    if not tenant_id:
        return []
    try:
        if sb is None:
            from ingestion.scraper import get_supabase

            sb = get_supabase()
        rows = (
            sb.table("intake_checklists")
            .select("checklist_id, label, match, signals, priority")
            .eq("tenant_id", str(tenant_id))
            .eq("enabled", True)
            .execute()
            .data
            or []
        )
        return [r for r in rows if isinstance(r.get("signals"), list) and r["signals"]]
    except Exception as e:  # noqa: BLE001
        log.warning("intake.load_checklists(%s): %s", tenant_id, e)
        return []


def _haystack(state: dict) -> str:
    case = state.get("case", {}) or {}
    cls = state.get("classification", {}) or {}
    parts = [
        case.get("subject"),
        case.get("body"),
        cls.get("topic"),
        cls.get("summary"),
        state.get("attachment_text"),
    ]
    return "\n".join(p for p in parts if p).lower()


def _attachment_kinds(state: dict) -> set[str]:
    return {
        (a.get("kind") or "image")
        for a in (state.get("attachments") or [])
        if not a.get("skipped")
    }


def _match_score(match: dict, state: dict) -> int | None:
    """`None` if any stated condition fails; else how many conditions held
    (a bigger number = a more specific checklist)."""
    if not isinstance(match, dict):
        return None
    cls = state.get("classification", {}) or {}
    case = state.get("case", {}) or {}
    hay = _haystack(state)
    score = 0

    def _eq(a: Any, b: Any) -> bool:
        return str(a or "").strip().lower() == str(b or "").strip().lower()

    for key, cur in (
        ("module", cls.get("module") or case.get("module")),
        ("submodule", cls.get("submodule") or case.get("submodule")),
        ("case_type", cls.get("case_type")),
        ("topic", cls.get("topic")),
    ):
        want = match.get(key)
        if want in (None, "", []):
            continue
        if _eq(cur, want):
            score += 1
        else:
            return None

    kws = match.get("keywords") or []
    if kws:
        if any(str(k).strip().lower() in hay for k in kws if str(k).strip()):
            score += 1
        else:
            return None

    return score


def checklist_for(state: dict, *, sb=None) -> dict | None:
    """The best-matching enabled checklist for this case, or `None`."""
    rows = load_checklists(state.get("tenant_id"), sb=sb)
    best: tuple[int, int, dict] | None = None
    for r in rows:
        s = _match_score(r.get("match") or {}, state)
        if s is None:
            continue
        rank = (s, int(r.get("priority") or 0))
        if best is None or rank > best[:2]:
            best = (rank[0], rank[1], r)
    return best[2] if best else None


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
def _detect_one(rule: dict, text: str, kinds: set[str]) -> bool:
    if not isinstance(rule, dict) or not rule:
        return False
    if "attachment_type" in rule:
        return str(rule["attachment_type"] or "image") in kinds
    if "any_of" in rule:
        return any(str(t).strip().lower() in text for t in (rule["any_of"] or []) if str(t).strip())
    if "regex" in rule:
        try:
            return re.search(rule["regex"], text, re.I) is not None
        except re.error:
            return False
    return False


def _sf_field_value(signal: dict, state: dict) -> str | None:
    """A value the checklist can already read off Salesforce (an
    `sf_context` node ran) — so we neither ask for it nor re-write it."""
    dest = (signal.get("lands_in") or {}).get("sf_field")
    if not dest:
        return None
    for bag in (
        (state.get("sf_context") or {}).get("case") or {},
        (state.get("case") or {}).get("fields") or {},
        state.get("case") or {},
    ):
        v = bag.get(dest)
        if v not in (None, "", []):
            return str(v)[:_VALUE_MAX]
    return None


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
_SYS = (
    "You are triaging one support case. For each detail requested, return its "
    "value ONLY if the customer's message (or text read from their screenshot) "
    "actually provides it — a short phrase, no invention. Use null when it is "
    'not stated. Return a JSON object: {"<key>": <string|null>, ...}.'
)


def _llm_fill(signals: list[dict], state: dict, *, vision: bool) -> dict[str, str]:
    if not signals:
        return {}
    case = state.get("case", {}) or {}
    asked = "\n".join(
        f'- {s["key"]}: {s.get("label") or s.get("question") or s["key"]}'
        for s in signals
    )
    body = f"Subject: {case.get('subject', '')}\n\n{case.get('body', '')}".strip()
    at = (state.get("attachment_text") or "").strip()
    if at and not vision:
        body += f"\n\n--- text read from attached image(s) ---\n{at[:2000]}"
    user = f"# Details to extract\n{asked}\n\n# Customer case\n{body or '(empty)'}"

    images = None
    if vision:
        blobs = state.get("_attachment_blobs") or {}
        images = [
            (b, "image/png") for b in list(blobs.values())[:3] if isinstance(b, (bytes, bytearray))
        ] or None
        if images is None:
            return {}

    try:
        raw = llm.complete(
            _SYS, user, model=None if vision else _MODEL, json_object=True,
            max_tokens=220, images=images, tenant_id=state.get("tenant_id"),
        )
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("intake._llm_fill(vision=%s): %s", vision, e)
        return {}
    if not isinstance(data, dict):
        return {}

    want = {s["key"] for s in signals}
    out: dict[str, str] = {}
    for k, v in data.items():
        if k in want and isinstance(v, str) and v.strip() and v.strip().lower() != "null":
            out[k] = v.strip()[:_VALUE_MAX]
    return out


def extract(checklist: dict, state: dict, *, use_llm: bool = True) -> dict:
    """`{"known": {key: value}, "sources": {key: "detect"|"sf_field"|"llm"|"vision"}}`."""
    signals = [s for s in (checklist.get("signals") or []) if s.get("key")]
    text = _haystack(state)
    kinds = _attachment_kinds(state)

    known: dict[str, str] = {}
    sources: dict[str, str] = {}

    for s in signals:
        k = s["key"]
        sf_val = _sf_field_value(s, state)
        if sf_val is not None:
            known[k], sources[k] = sf_val, "sf_field"
            continue
        if _detect_one(s.get("detect") or {}, text, kinds):
            known[k], sources[k] = "present", "detect"

    if use_llm:
        pending = [s for s in signals if s["key"] not in known]
        for k, v in _llm_fill(pending, state, vision=False).items():
            known[k], sources[k] = v, "llm"

        vis = [
            s for s in signals
            if s["key"] not in known and s.get("vision") and (state.get("_attachment_blobs"))
        ]
        for k, v in _llm_fill(vis, state, vision=True).items():
            known[k], sources[k] = v, "vision"

    return {"known": known, "sources": sources}


# --------------------------------------------------------------------------- #
# gaps / questions / writeback
# --------------------------------------------------------------------------- #
def gaps(checklist: dict, known: dict) -> list[dict]:
    """Required signals with nothing known yet — required first, in spec order."""
    out = [
        s for s in (checklist.get("signals") or [])
        if s.get("key") and s.get("required", True) and s["key"] not in known
    ]
    return out


def questions_for(gap_signals: list[dict], limit: int = _MAX_QUESTIONS) -> list[str]:
    qs: list[str] = []
    for s in gap_signals:
        q = (s.get("question") or "").strip() or (
            f"Could you provide: {s.get('label') or s['key']}?"
        )
        if q not in qs:
            qs.append(q)
        if len(qs) >= max(1, limit):
            break
    return qs


def field_writes(checklist: dict, known: dict) -> dict[str, str]:
    """`{Salesforce field API name: value}` for the signals we learned that
    map to a real field (skip the sentinel `"present"` from a `detect` hit —
    an attachment being present isn't a field value)."""
    out: dict[str, str] = {}
    for s in checklist.get("signals") or []:
        k = s.get("key")
        dest = (s.get("lands_in") or {}).get("sf_field")
        v = known.get(k)
        if dest and v and v != "present":
            out[dest] = v
    return out
