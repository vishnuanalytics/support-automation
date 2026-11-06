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
from typing import Any

from .redact import redact_pii

log = logging.getLogger("interpreter.monitor.replies")

NOISE = ("ack", "internal_tag")
REAL = ("bug_raised", "solution", "needs_info", "update")
MAX_LLM_PER_SYNC = 40
_MAX_RULE_LENGTH = 90

_ACK = re.compile(
    r"^(hi|hello|hey|dear|team|thanks?|thank you|ok(ay)?|noted|sure|received|ack(nowledged)?|"
    r"we('| a)re|we will|i will|i'm|will|let me|please allow|allow|give|me|us|some time|"
    r"checking( on)?|check(ing)?|looking( into)?|into|look into|on it|get back|update|revert|"
    r"it|this|shortly|soon|[\s,.!:-])+$", re.I)
_INTERNAL_TAG = re.compile(r"^(\s*(<@[^>]+>|@[\w.-]+|\+\w+)[\s,]*)+(please|pls|kindly|can you|"
                           r"could you|fyi|cc|check|look|help|take)", re.I)

SYSTEM = (
    "You sort a reply from a delivery platform's support team, sent on a support case raised by "
    "a restaurant software company. Answer as JSON: {\"kind\": ..., \"summary\": ...}. "
    "kind is one of: ack (only acknowledges, or says they are checking), internal_tag (only tags "
    "or forwards to a colleague), bug_raised (raised to their technical team, or a bug is "
    "confirmed), solution (gives a fix, cause or outcome), needs_info (asks us for details), "
    "update (any other real progress). If unsure, use update. summary is one short sentence "
    "using only what the reply says."
)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def rule_kind(text: str | None) -> str | None:
    bare = re.sub(r"\s+", " ", text or "").strip()
    if not bare:
        return "ack"
    if len(bare) > _MAX_RULE_LENGTH:
        return None
    if _INTERNAL_TAG.search(bare):
        return "internal_tag"
    return "ack" if _ACK.match(bare) else None


def llm_kind(text: str, tenant_id: str, complete: Any = None) -> dict[str, str]:
    from interpreter import llm

    raw = (complete or llm.complete)(SYSTEM, redact_pii(text), max_tokens=150, json_object=True,
                                     tenant_id=tenant_id)
    body = json.loads(raw)
    kind = body.get("kind")
    if kind not in NOISE + REAL:
        kind = "update"
    return {"kind": kind, "summary": str(body.get("summary") or "")[:200], "method": "llm"}


def label_latest(tenant_id: str, rows: list[dict], store: Any, cfg: dict) -> None:
    """Attach `reply` to each case whose latest message is from the third party."""
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
        elif budget > 0:
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
