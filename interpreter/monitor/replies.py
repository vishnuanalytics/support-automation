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
NO_ANSWER = "no_answer"
REAL = ("bug_raised", "solution", "needs_info", "update", NO_ANSWER)
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
# Replied, but with nothing to act on: they can't see why, and ask for nothing specific.
_NO_ANSWER = re.compile(r"\b(unable to|not able to|can'?t|cannot|could ?n[o']t)\s+(locate|find|retrieve|fetch|get)\b"
                        r"[^.]{0,40}\b(logs?|reason)\b", re.I)
_ASKS_FOR_SOMETHING = re.compile(r"\b(re-?push|re-?publish|share|send|attach|provide us)\b", re.I)
_MAX_NO_ANSWER_LENGTH = 250
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
    "- no_answer: replies, but gives no cause, fix or specific request, so there's nothing to act "
    "on. Examples: 'We can't find the logs to give the reason', 'Please make sure the SOP is "
    "followed, we can't tell what failed'.\n"
    "- ack: only confirms receipt or that it's queued or being looked at, with no result. "
    "Examples: 'Looking into this, will update soon', 'The IDs are prioritized'.\n"
    "- internal_tag: addressed to a colleague at the platform, asking them to take a look. "
    "Examples: 'Hi Priya, can you look into this', '+Ravi please prioritize'.\n"
    "- bug_raised: says it's been raised to their technical team, or confirms a bug.\n"
    "- update: real progress that fits none of the above.\n"
    "A greeting to a support agent by name isn't internal_tag. summary is one short sentence using "
    "only what the reply says."
)


# The merchant's own latest message. Same contract: a label feeds the rules,
# never decides whose turn it is, and anything uncertain means "needs a reply".
CUSTOMER_CONFIRMED, CUSTOMER_THANKS, CUSTOMER_NEEDS_REPLY = "confirmed", "thanks", "needs_reply"
CUSTOMER_KINDS = (CUSTOMER_CONFIRMED, CUSTOMER_THANKS, CUSTOMER_NEEDS_REPLY)
_CUSTOMER_KEY = "customer:"
_CUSTOMER_MAX_RULE_LENGTH = 80
_THANKS = re.compile(r"^(hi|hello|hey|dear|team|ok(ay)?|sure|noted|got it|great|perfect|"
                     r"thanks?( a lot| so much)?|thank you( so much| very much)?|ty|"
                     r"[\s,.!:-])+$", re.I)
# A greeting alone ("Hi") is usually a cut-off email or an image, not thanks.
_THANKS_WORD = re.compile(r"\b(ok(ay)?|sure|noted|got it|great|perfect|thanks?|thank you|ty)\b", re.I)
# Short, unhedged "it works now" / "you can close". A question mark, "still", "not" or
# "but" anywhere sends it to the LLM instead, since those usually mean it isn't done.
_CONFIRMED = re.compile(r"\b(it'?s|it is|its|now|all)\s+(working|fixed|resolved|showing|visible|fine|good)\b|"
                        r"\b(issue|problem)\s+(is\s+)?(resolved|fixed|sorted)\b|"
                        r"\b(working|fixed|resolved|showing|visible|fine)\s+now\b|"
                        r"\b(you\s+)?(can|may)\s+close\b|\bworking fine\b", re.I)
_HEDGE = re.compile(r"\?|\b(still|not|n't|but|however|again|except)\b", re.I)

CUSTOMER_SYSTEM = (
    "You sort one email a restaurant (the merchant) sent to its software vendor's support team "
    "about an open support case. Decide what it means for that support team.\n"
    "Answer as JSON: {\"kind\": ..., \"summary\": ...}. Kinds:\n"
    "- confirmed: the merchant says the issue is fixed, working, resolved, or the case can be "
    "closed, with nothing still wrong. Examples: 'Images are showing now, thanks', 'All good, you "
    "can close this', 'Orders are coming through again'.\n"
    "- thanks: only thanks or an acknowledgement, with no new problem, question or request. "
    "Examples: 'Thanks for the update', 'Noted, will wait', 'Ok sure'.\n"
    "- needs_reply: anything else: the problem persists or came back, a new issue, a question, "
    "a complaint, details or a request, or a mix of thanks and any of those.\n"
    "When unsure, answer needs_reply. summary is one short sentence using only what the email says."
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
    if len(bare) <= _MAX_NO_ANSWER_LENGTH and _NO_ANSWER.search(bare) and not _ASKS_FOR_SOMETHING.search(bare):
        return NO_ANSWER
    if len(bare) > _MAX_RULE_LENGTH:
        return None
    if _MENTIONS.match(bare) or _SLACK_MENTION.search(bare):
        return "internal_tag"
    return "ack" if _ACK.match(bare) or _QUEUED.search(bare) else None


def customer_key(text: str) -> str:
    """Merchant labels live in the same table under their own hashes."""
    return text_hash(_CUSTOMER_KEY + text)


def _is_thanks(bare: str) -> bool:
    return bool(_THANKS.match(bare) and _THANKS_WORD.search(bare))


def customer_rule_kind(text: str | None) -> str | None:
    bare = re.sub(r"\s+", " ", core_text(text)).strip()
    if not bare:
        # A bare "Thanks!" is all sign-off, so core_text leaves nothing: judge the raw text.
        bare = re.sub(r"\s+", " ", text or "").strip()
        return CUSTOMER_THANKS if bare and len(bare) <= 30 and _is_thanks(bare) else None
    if len(bare) > _CUSTOMER_MAX_RULE_LENGTH:
        return None
    if _is_thanks(bare):
        return CUSTOMER_THANKS
    if _CONFIRMED.search(bare) and not _HEDGE.search(bare):
        return CUSTOMER_CONFIRMED
    return None


def customer_llm_kind(text: str, tenant_id: str, complete: Any = None) -> dict[str, str]:
    from interpreter import llm

    raw = (complete or llm.complete)(CUSTOMER_SYSTEM, redact_pii(core_text(text))[:1500],
                                     max_tokens=200, json_object=True, tenant_id=tenant_id,
                                     model=llm.FAST_MODEL)
    body = json.loads(raw)
    kind = body.get("kind")
    if kind not in CUSTOMER_KINDS:
        kind = CUSTOMER_NEEDS_REPLY
    return {"kind": kind, "summary": str(body.get("summary") or "")[:200], "method": "llm"}


def label_customer(tenant_id: str, rows: list[dict], store: Any, cfg: dict,
                   clock: Any = time.monotonic) -> None:
    """Attach `customer_reply` to each case whose latest message is the merchant's.

    Unlabelled (no budget, LLM off, or a failed call) means no `customer_reply`,
    which the verdict treats exactly as before: a merchant reply needs us."""
    deadline = clock() + LABEL_SECONDS_PER_SYNC
    pending: dict[str, list[dict]] = {}
    for case, latest in rows:
        if latest and latest.get("party") == "customer" and latest.get("text"):
            pending.setdefault(customer_key(latest["text"]), []).append({"case": case, "m": latest})
    if not pending:
        return
    known = store.reply_labels(tenant_id, list(pending))
    fresh: dict[str, dict] = {}
    budget = MAX_LLM_PER_SYNC if cfg.get("llm_enabled") else 0
    for key, group in pending.items():
        text = group[0]["m"]["text"]
        kind = customer_rule_kind(text)
        if not kind and (known.get(key) or {}).get("method") == "rule":
            known.pop(key)   # the rules changed and no longer settle this text: label it afresh
        if kind:
            if (known.get(key) or {}).get("kind") != kind:
                fresh[key] = {"kind": kind, "summary": "", "method": "rule"}
        elif key not in known and budget > 0 and clock() < deadline:
            budget -= 1
            try:
                fresh[key] = customer_llm_kind(text, tenant_id)
            except Exception as e:  # noqa: BLE001 -- unlabelled means it needs a reply
                log.warning("customer reply label failed: %s", type(e).__name__)
    store.save_reply_labels(tenant_id, fresh)
    labels = {**known, **fresh}
    for key, group in pending.items():
        label = labels.get(key)
        if label and label.get("kind") in CUSTOMER_KINDS:
            for item in group:
                item["case"]["customer_reply"] = {"kind": label["kind"], "summary": label.get("summary"),
                                                  "at": item["m"].get("at")}


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
        text = group[0]["m"]["text"]
        kind = rule_kind(text)
        if kind:
            # A rule outranks an older AI label on the same text.
            if (known.get(key) or {}).get("kind") != kind:
                fresh[key] = {"kind": kind, "summary": "", "method": "rule"}
        elif key not in known and budget > 0 and clock() < deadline:
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
