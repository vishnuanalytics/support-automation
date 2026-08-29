"""
Phase 11 — did the human keep the bot's draft?

`classify_edit(draft, human_reply)` -> (action, edit_distance):
    sent_as_is  ratio >= 0.97
    edited      ratio >= 0.55
    rewrote     ratio <  0.55
edit_distance = 1 - ratio  (0 identical, 1 unrelated).

`fetch_human_reply(sf, case_id)` -> the human's actual outbound text:
the latest non-incoming EmailMessage on the Case, else the latest
CaseComment, else None (-> the run is marked `no_reply`).
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").strip()).lower()


def classify_edit(draft: str, human_reply: str) -> tuple[str, float]:
    d, h = _norm(draft), _norm(human_reply)
    if not h:
        return "no_reply", 1.0
    if not d:
        return "rewrote", 1.0
    ratio = SequenceMatcher(None, d, h).ratio()
    action = "sent_as_is" if ratio >= 0.97 else "edited" if ratio >= 0.55 else "rewrote"
    return action, round(1.0 - ratio, 4)


_EMAIL_SOQL = (
    "SELECT TextBody, Subject, CreatedDate FROM EmailMessage "
    "WHERE ParentId = '{cid}' AND Incoming = false "
    "ORDER BY CreatedDate DESC LIMIT 1"
)
_COMMENT_SOQL = (
    "SELECT CommentBody, CreatedDate FROM CaseComment "
    "WHERE ParentId = '{cid}' ORDER BY CreatedDate DESC LIMIT 1"
)


def fetch_human_reply(sf, case_id: str) -> str | None:
    for soql, field in ((_EMAIL_SOQL, "TextBody"), (_COMMENT_SOQL, "CommentBody")):
        try:
            rows = sf.query(soql.format(cid=case_id)).get("records", [])
        except Exception:  # noqa: BLE001 -- object not available in this org
            continue
        if rows and rows[0].get(field):
            return rows[0][field]
    return None
