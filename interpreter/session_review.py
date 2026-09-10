"""
Response Quality Feedback Loop chunk C (2026-09-10) — score whole
`reasoning_sessions` transcripts (migration `056`): the actual multi-turn
"to and fro" between the bot and the responsible agent that the user
originally flagged as unlabelled. Chunks A/B judge one (draft, human_reply)
pair at a time; this judges the *dialogue itself* — did the bot ask sharp,
non-redundant questions and converge on a correct draft, or did it loop,
misread the issue, or get abandoned needlessly?

Same judge-routing approach as `interpreter/correction_review.py` (single
Groq-routed `interpreter/llm.py` call, structured JSON) — kept as its own
module rather than folded into `correction_review.py` because the input
shape (a multi-turn transcript + pointer Q&A, not one draft/reply pair)
and the category set are genuinely different signals.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("interpreter.session_review")

CATEGORIES = ("sound", "redundant", "misguided", "abandoned", "other")

_SYSTEM = (
    "You review a transcript of a customer-support bot reasoning through an "
    "escalated case together with a human agent, before a reply is sent to "
    "the customer. The bot asks the agent a bank of pointer questions, "
    "drafts a reply, and only sends on the agent's approval. Judge the "
    "QUALITY OF THE DIALOGUE ITSELF, not the final wording.\n"
    "Categories:\n"
    "sound — the bot asked sharp, non-redundant questions, understood the "
    "issue, and converged on a correct draft efficiently.\n"
    "redundant — the bot re-asked things the agent already answered, or "
    "took more rounds than the issue warranted.\n"
    "misguided — the bot's questions or draft were off-track relative to "
    "what the case was actually about.\n"
    "abandoned — the agent held the case back (declined to send) and the "
    "transcript suggests the dialogue itself is why (wrong understanding, "
    "unresolved gap), not an unrelated reason.\n"
    "other — none of the above fit, including a sound dialogue the agent "
    "simply abandoned for reasons unrelated to the bot's performance.\n"
    'Reply with ONLY a JSON object: {"category": <one of the above>, '
    '"severity": <0.0-1.0, how much this hurt the outcome or wasted the '
    'agent\'s time>, "summary": "<one specific sentence>"}.'
)


def _format_transcript(transcript: list) -> str:
    lines = []
    for turn in transcript or []:
        role = str(turn.get("role") or "?").upper()
        text = str(turn.get("text") or "").strip()
        if text:
            lines.append(f"{role}: {text}")
    return "\n\n".join(lines)


def _format_pointers(pointers: list) -> str:
    lines = []
    for p in pointers or []:
        q = p.get("q") or ""
        note = p.get("agent_note") or p.get("bot_take") or ""
        if q:
            lines.append(f"- {q}" + (f" -> {note}" if note else " -> (unanswered)"))
    return "\n".join(lines)


def judge_session(transcript: list, *, pointers: "list | None" = None,
                  draft: "str | None" = None, state: "str | None" = None,
                  subject: str = "", tenant_id: "str | None" = None) -> dict:
    """Best-effort — never raises. Returns `{"category", "severity",
    "summary"}`. `category="unknown"` on any failure (empty transcript,
    judge unavailable, malformed response) — kept distinct from the real
    verdict `category="other"` so a caller can tell "judged, doesn't fit a
    bucket" apart from "couldn't judge it"."""
    from interpreter import llm

    convo = _format_transcript(transcript)
    if not convo.strip():
        return {"category": "unknown", "severity": None, "summary": "empty transcript"}

    parts = [f"Case: {subject or '(no subject)'}", f"# Dialogue\n{convo}"]
    ptext = _format_pointers(pointers or [])
    if ptext:
        parts.append(f"# Pointer questions & agent answers\n{ptext}")
    if draft:
        parts.append(f"# Final draft\n{draft}")
    parts.append(f"# Outcome\n{state or '(unknown)'}")
    user = "\n\n".join(parts)

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
        log.warning("judge_session failed: %s", e)
        return {"category": "unknown", "severity": None, "summary": ""}
