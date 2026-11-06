"""Sort a third party's reply into noise or a real update.

Clear acknowledgements are caught by rules, for free. The rest go to an LLM.
Anything uncertain counts as a real update, so a failure can only show a reply
that didn't need attention, never hide one that did.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any

from .redact import redact_pii

log = logging.getLogger("interpreter.monitor.replies")

NOISE = ("ack", "internal_tag")
REAL = ("bug_raised", "solution", "needs_info", "update")
MAX_LLM_PER_SYNC = 40
# AI limits can stall a call for minutes, and that mustn't hold up a sync. Whatever
# isn't labelled in time counts as real, and the next sync tries again.
LABEL_SECONDS_PER_SYNC = 45
_MAX_RULE_LENGTH = 90

_QUEUED = re.compile(r"\b(are|is|have been|has been)\s+(prioriti[sz]ed|priortized|queued)\b\.?$", re.I)
_ACK = re.compile(
    r"^(hi|hello|hey|dear|team|thanks?|thank you|ok(ay)?|noted|sure|received|ack(nowledged)?|"
    r"we('| a)re|we will|i will|i'm|will|let me|please allow|allow|give|me|us|some time|"
    r"currently|checking( on)?|check(ing)?|looking( into)?|into|look into|on it|get back|update|"
    r"revert|you|soon|issue|the|it|this|shortly|and|[\s,.!:-])+$", re.I)
# Only names or mentions, then at most a short request: forwarding to a colleague.
_MENTIONS = re.compile(r"^([@+][^\n,:<]{2,40}(<[^>]*>)?[\s,:-]*)+"
                       r"((please|pls|kindly)?\s*(assist|check|help|look)[\w\s]{0,20})?[.!]?$", re.I)
_SLACK_MENTION = re.compile(r"^(\s*<@[^>]+>[\s,]*)+(please|pls|kindly|can you|could you|fyi|cc|"
                            r"check|look|help|take)", re.I)
_SIGN_OFF = re.compile(r"^\W*((best|warm|kind) regards|thanks (&|and) regards|regards\W*$|"
                       r"thanks\W*$|thank you\W*$|cheers\W*$)", re.I)
_SIGN_OFF_LINES = 5

SYSTEM = (
    "You sort one reply from a delivery platform's team, on a case raised by a restaurant software "
    "company's support team. Decide what the reply means for that support team.\n"
    "Answer as JSON: {\"kind\": ..., \"summary\": ...}. Kinds:\n"
    "- solution: states a cause, fact, answer or outcome, even in one line. Examples: 'This is a "
    "scheduled order', 'The outlet is offline due to high rejections', 'The item is now approved', "
    "'Logs show it was delivered to your end', 'We can't provide an update on this', 'Feature "
    "enabled, please check'.\n"
    "- needs_info: asks the support team for details, or to do or check something. Examples: "
    "'Please share the modifier names', 'This ID doesn't belong to that brand, please check', "
    "'Please align on adding the tag', 'Kindly recheck'.\n"
    "- ack: only confirms receipt or that it's queued or being looked at, with no result. "
    "Examples: 'Looking into this, will update soon', 'The IDs are prioritized'.\n"
    "- internal_tag: addressed to a colleague at the platform, asking them to take a look. "
    "Examples: 'Hi Priya, can you look into this', '+Ravi please prioritize'.\n"
    "- bug_raised: says it's been raised to their technical team, or confirms a bug.\n"
    "- update: real progress that fits none of the above.\n"
    "A greeting to a support agent by name isn't internal_tag. summary is one short sentence using "
    "only what the reply says."
)


def core_text(text: str | None) -> str:
    """Only what this reply says: no quoted thread, footer or sign-off. A sign-off
    only counts near the end, so "Thank you for reaching out." keeps what follows."""
    from .knowledge import strip_quoted

    lines = strip_quoted(text).splitlines()
    for i in range(max(0, len(lines) - _SIGN_OFF_LINES), len(lines)):
        if _SIGN_OFF.match(lines[i]):
            lines = lines[:i]
            break
    return "\n".join(lines).strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def rule_kind(text: str | None) -> str | None:
    bare = re.sub(r"\s+", " ", core_text(text)).strip()
    if not bare:
        return "ack"
    if len(bare) > _MAX_RULE_LENGTH:
        return None
    if _MENTIONS.match(bare) or _SLACK_MENTION.search(bare):
        return "internal_tag"
    return "ack" if _ACK.match(bare) or _QUEUED.search(bare) else None


def llm_kind(text: str, tenant_id: str, complete: Any = None) -> dict[str, str]:
    from interpreter import llm

    raw = (complete or llm.complete)(SYSTEM, redact_pii(core_text(text))[:1500], max_tokens=200,
                                     json_object=True, tenant_id=tenant_id, model=llm.FAST_MODEL)
    body = json.loads(raw)
    kind = body.get("kind")
    if kind not in NOISE + REAL:
        kind = "update"
    return {"kind": kind, "summary": str(body.get("summary") or "")[:200], "method": "llm"}


def label_latest(tenant_id: str, rows: list[dict], store: Any, cfg: dict,
                 clock: Any = time.monotonic) -> None:
    """Attach `reply` to each case whose latest message is from the third party."""
    deadline = clock() + LABEL_SECONDS_PER_SYNC
    pending: dict[str, list[dict]] = {}
    for case, latest in rows:
        if latest and latest.get("party") == "third_party" and latest.get("text"):
            pending.setdefault(text_hash(latest["text"]), []).append({"case": case, "m": latest})
    if not pending:
        return
    known = store.reply_labels(tenant_id, list(pending))
    fresh: dict[str, dict] = {}
    budget = MAX_LLM_PER_SYNC if cfg.get("llm_enabled") else 0
    for key, group in pending.items():
        if key in known:
            continue
        text = group[0]["m"]["text"]
        kind = rule_kind(text)
        if kind:
            fresh[key] = {"kind": kind, "summary": "", "method": "rule"}
        elif budget > 0 and clock() < deadline:
            budget -= 1
            try:
                fresh[key] = llm_kind(text, tenant_id)
            except Exception as e:  # noqa: BLE001 -- unlabelled means treated as real
                log.warning("reply label failed: %s", type(e).__name__)
    store.save_reply_labels(tenant_id, fresh)
    labels = {**known, **fresh}
    for key, group in pending.items():
        if key in labels:
            for item in group:
                item["case"]["reply"] = {k: labels[key].get(k) for k in ("kind", "summary")}
