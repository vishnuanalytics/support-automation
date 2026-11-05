"""Read-only clients for the case system, GitHub and Slack.

Salesforce and GitHub only ever issue reads. On Slack the only methods used
here are `conversations.replies` and `users.info`; the single write in this
feature is `post_team_tag`, reachable only from a person's click.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

import requests

from .redact import clean

log = logging.getLogger("interpreter.monitor.readers")

CONCURRENCY = 8
MAX_RETRIES = 2
MAX_RETRY_WAIT_S = 30.0
TIMEOUT_S = 20
_SLACK_API = "https://slack.com/api"
_MENTION = re.compile(r"<[@!#][^>]*>")
_RECONNECT = ("invalid_auth", "token_revoked", "token_expired", "account_inactive")


class EvidenceReader(Protocol):
    """One linked system. A future Jira reader implements this."""

    def fetch(self, link: dict[str, Any]) -> dict[str, Any]: ...


def _get(url: str, headers: dict, params: dict | None = None) -> requests.Response:
    for attempt in range(MAX_RETRIES + 1):
        r = requests.get(url, headers=headers, params=params, timeout=TIMEOUT_S)
        if r.status_code != 429 or attempt == MAX_RETRIES:
            return r
        time.sleep(min(float(r.headers.get("Retry-After") or 1), MAX_RETRY_WAIT_S))
    return r


class GithubReader:
    def __init__(self, token: str | None, support_users: list[str]):
        self.token = token
        self.support = {u.lower() for u in support_users}

    def fetch(self, link: dict[str, Any]) -> dict[str, Any]:
        repo = f"{link['owner']}/{link['repo']}"
        base = {"kind": "github", "url": link["url"], "label": f"{repo}#{link['number']}"}
        denied = {**base, "ok": False, "error": f"not accessible: check token scope for {repo}"}
        if not self.token:
            return {**denied, "error": "GitHub is not connected for this workspace"}
        headers = {"Authorization": f"Bearer {self.token}",
                   "Accept": "application/vnd.github+json"}
        api = f"https://api.github.com/repos/{repo}/issues/{link['number']}"
        issue = _get(api, headers)
        if issue.status_code in (401, 403, 404):
            return denied
        issue.raise_for_status()
        body = issue.json()
        author = (body.get("user") or {}).get("login", "").lower()

        comments: list[dict] = []
        page = 1
        while True:
            r = _get(f"{api}/comments", headers, {"per_page": 100, "page": page})
            if r.status_code in (401, 403, 404):
                return denied
            r.raise_for_status()
            rows = r.json()
            comments += rows
            if len(rows) < 100:
                break
            page += 1

        messages = []
        for c in comments:
            user = c.get("user") or {}
            if user.get("type") == "Bot":
                continue
            login = user.get("login", "")
            messages.append({"who": login, "at": c.get("created_at"), "url": c.get("html_url"),
                             "ours": login.lower() == author or login.lower() in self.support,
                             "text": clean(c.get("body"))})
        return {**base, "ok": True, "state": body.get("state"), "title": clean(body.get("title")),
                "labels": [x.get("name") for x in body.get("labels") or []],
                "updated_at": body.get("updated_at"), "messages": messages}


class SlackError(Exception):
    pass


_SLACK_HELP = {
    "channel_not_found": "channel not visible: join the channel with the connected Slack user",
    "not_in_channel": "not in this channel: join it with the connected Slack user",
    "missing_scope": "missing Slack scope: reconnect Slack in Case Monitor",
    "thread_not_found": "thread not found: it may have been deleted",
    **dict.fromkeys(_RECONNECT, "Slack login expired: reconnect Slack in Case Monitor"),
}


class SlackReader:
    """Reads threads with connected leads' user tokens, trying each in turn
    because different leads belong to different external channels."""

    def __init__(self, tokens: list[str], internal_domain: str):
        self.tokens = tokens
        self.domain = internal_domain.lower().lstrip("@")
        self._emails: dict[str, str] = {}

    def _call(self, method: str, token: str, params: dict) -> dict:
        r = _get(f"{_SLACK_API}/{method}", {"Authorization": f"Bearer {token}"}, params)
        r.raise_for_status()
        body = r.json()
        if not body.get("ok"):
            raise SlackError(body.get("error") or "unknown_error")
        return body

    def _user(self, uid: str, token: str) -> tuple[str, str]:
        cached = self._emails.get(uid)
        if cached is None:
            try:
                user = self._call("users.info", token, {"user": uid})["user"]
                profile = user.get("profile") or {}
                cached = f"{profile.get('email', '')}|{user.get('real_name') or user.get('name') or uid}"
            except SlackError:
                cached = f"|{uid}"
            self._emails[uid] = cached
        email, name = cached.split("|", 1)
        return email, name

    def _is_noise(self, text: str) -> bool:
        bare = _MENTION.sub("", text or "").strip().lower()
        return not bare or bare == "cc"

    def _messages(self, link: dict, token: str) -> list[dict]:
        rows: list[dict] = []
        cursor = None
        while True:
            params = {"channel": link["channel"], "ts": link["thread_ts"], "limit": 200}
            if cursor:
                params["cursor"] = cursor
            body = self._call("conversations.replies", token, params)
            rows += body.get("messages") or []
            cursor = (body.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        host = link["url"].split("/archives/")[0]
        out = []
        for m in rows:
            if not m.get("user") or self._is_noise(m.get("text", "")):
                continue
            email, name = self._user(m["user"], token)
            ts = m["ts"]
            url = (f"{host}/archives/{link['channel']}/p{ts.replace('.', '')}"
                   f"?thread_ts={link['thread_ts']}")
            ours = bool(self.domain) and email.lower().endswith(f"@{self.domain}")
            out.append({"who": name, "ours": ours, "url": url, "text": clean(m.get("text")),
                        "at": _slack_iso(ts)})
        return out

    def fetch(self, link: dict[str, Any]) -> dict[str, Any]:
        base = {"kind": "slack", "url": link["url"], "label": "Slack thread"}
        if not link.get("channel"):
            return {**base, "ok": False, "error": "not a message link: paste a thread permalink"}
        if not self.tokens:
            return {**base, "ok": False, "error": "connect Slack in Case Monitor to read this"}
        code = "unknown_error"
        for token in self.tokens:
            try:
                return {**base, "ok": True, "messages": self._messages(link, token)}
            except SlackError as e:
                code = str(e)
        return {**base, "ok": False, "error": _SLACK_HELP.get(code, f"Slack error: {code}")}


def _slack_iso(ts: str) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def post_team_tag(token: str, *, channel: str, thread_ts: str, text: str) -> dict:
    """Post one thread reply as the person who clicked Tag team."""
    r = requests.post(f"{_SLACK_API}/chat.postMessage",
                      headers={"Authorization": f"Bearer {token}"},
                      json={"channel": channel, "thread_ts": thread_ts, "text": text},
                      timeout=TIMEOUT_S)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise SlackError(_SLACK_HELP.get(body.get("error"), f"Slack error: {body.get('error')}"))
    return {"ok": True, "ts": body.get("ts")}


class EvidenceCollector:
    """Per-sync cache by URL, bounded concurrency, one bad link never spreads."""

    def __init__(self, readers: dict[str, EvidenceReader]):
        self.readers = readers
        self.cache: dict[str, dict] = {}

    def _one(self, link: dict) -> dict:
        kind = link["kind"]
        reader = self.readers.get(kind)
        if reader is None:
            label = {"jira": "Jira", "freshchat": "Freshchat"}.get(kind, "Link")
            return {"kind": kind, "url": link["url"], "label": label, "ok": False, "error": None,
                    "note": "linked, status not checked"}
        try:
            return reader.fetch(link)
        except Exception as e:  # noqa: BLE001 -- one bad link must not fail the sync
            log.warning("evidence %s failed: %s", kind, type(e).__name__)
            return {"kind": kind, "url": link["url"], "label": kind, "ok": False,
                    "error": f"could not read {kind} link ({type(e).__name__})"}

    def collect(self, links: list[dict]) -> dict[str, dict]:
        todo = {x["url"]: x for x in links if x["url"] not in self.cache}
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            for url, ev in zip(todo, pool.map(self._one, todo.values())):
                self.cache[url] = ev
        return self.cache


def merge_links(*groups: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for group in groups:
        for link in group:
            seen.setdefault(link["url"], link)
    return list(seen.values())


_RECORD_TYPE_COLUMN = re.compile(r"(Parent\.)?RecordType\.Name, ")


def _lit(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _dig(row: dict, path: str) -> Any:
    cur: Any = row
    for part in path.split("."):
        cur = (cur or {}).get(part) if isinstance(cur, dict) else None
    return cur


class SalesforceCaseReader:
    """Only ever runs SOQL through `query_all`, so it cannot write."""

    BASE = ("Id", "CaseNumber", "Subject", "Description", "Priority", "Status", "RecordType.Name",
            "Owner.Name", "CreatedDate", "LastModifiedDate", "Reason", "ParentId",
            "Parent.CaseNumber", "Parent.Status", "Parent.RecordType.Name", "ContactEmail")

    def __init__(self, sf: Any, cfg: dict):
        self.sf = sf
        self.cfg = cfg
        self.fields = cfg["fields"]
        self.has_record_types = True

    def _select(self) -> str:
        names = list(self.BASE) + [f for f in self.fields.values() if f]
        if self.fields.get("link"):
            names.append(f"Parent.{self.fields['link']}")
        return ", ".join(dict.fromkeys(names))

    def _base_url(self) -> str:
        if self.cfg.get("lightning_url"):
            return self.cfg["lightning_url"].rstrip("/")
        host = getattr(self.sf, "sf_instance", "") or ""
        return f"https://{host}" if host else ""

    def to_case(self, row: dict) -> dict[str, Any]:
        from .links import parse

        f = self.fields
        value = lambda key: _dig(row, f[key]) if f.get(key) else None  # noqa: E731
        raw_wait = value("waiting_on")
        last = value("last_reply_from")
        reply_from = None
        if last:
            reply_from = "customer" if last in self.cfg["customer_reply_values"] else "us"
        third = value("third_party")
        case_id = row["Id"]
        base = self._base_url()
        parent = None
        if row.get("ParentId"):
            p = row.get("Parent") or {}
            parent = {"id": row["ParentId"], "case_number": p.get("CaseNumber"),
                      "status": p.get("Status"),
                      "record_type": (p.get("RecordType") or {}).get("Name"),
                      "url": f"{base}/lightning/r/Case/{row['ParentId']}/view" if base else None}
        return {
            "id": case_id, "source": "salesforce", "case_number": row.get("CaseNumber"),
            "subject": clean(row.get("Subject")), "description": clean(row.get("Description")),
            "priority": row.get("Priority"), "status": row.get("Status"),
            "record_type": (row.get("RecordType") or {}).get("Name"),
            "owner": (row.get("Owner") or {}).get("Name"),
            "created_at": row.get("CreatedDate"), "last_modified_at": row.get("LastModifiedDate"),
            "url": f"{base}/lightning/r/Case/{case_id}/view" if base else None,
            "waiting_raw": raw_wait, "waiting_party": self.cfg["waiting_values"].get(raw_wait),
            "sub_status": value("waiting_sub_status"), "last_reply_from": reply_from,
            "links": merge_links(parse(value("link")),
                                  parse(_dig(row, f"Parent.{f['link']}") if f.get("link") else None)),
            "third_parties": [x for x in (third or "").split(";") if x],
            "target_at": value("target_at"), "ageing": value("ageing"), "brand": value("brand"),
            "biz_id": value("biz_id"), "business_id": value("business_id"),
            "product": value("product"), "module": value("module"),
            "sub_module": value("sub_module"), "case_type": value("case_type"),
            "reason": row.get("Reason"), "escalation_level": value("escalation_level"),
            "escalated": value("escalated"), "contact_email": row.get("ContactEmail"),
            "parent": parent, "children": [],
        }

    def _records(self, soql: str) -> list[dict]:
        """An org with no record types has no RecordType relationship at all,
        so drop those columns and remember that for the rest of the sync."""
        if not self.has_record_types:
            soql = _RECORD_TYPE_COLUMN.sub("", soql)
        try:
            return self.sf.query_all(soql).get("records", [])
        except Exception as e:  # noqa: BLE001
            if not self.has_record_types or "RecordType" not in soql \
                    or "INVALID_FIELD" not in str(e):
                raise
        self.has_record_types = False
        return self._records(soql)

    def list_dependency_cases(self) -> list[dict]:
        waiting = self.fields.get("waiting_on")
        values = list(self.cfg["waiting_values"])
        if not waiting or not values:
            raise ValueError("Case Monitor needs fields.waiting_on and waiting_values configured")
        in_list = ", ".join(f"'{_lit(v)}'" for v in values)
        soql = (f"SELECT {self._select()} FROM Case "
                f"WHERE IsClosed = false AND {waiting} IN ({in_list})")
        owner = self.cfg.get("owner_username")
        if owner:
            soql += f" AND OwnerId IN (SELECT Id FROM User WHERE Username = '{_lit(owner)}')"
        return [self.to_case(r) for r in self._records(soql)]

    def get_cases(self, ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for start in range(0, len(ids), 200):
            chunk = ", ".join(f"'{_lit(i)}'" for i in ids[start:start + 200])
            out += [self.to_case(r) for r in
                    self._records(f"SELECT {self._select()} FROM Case WHERE Id IN ({chunk})")]
        return out

    def children_of(self, ids: list[str]) -> dict[str, list[dict]]:
        from .links import parse

        out: dict[str, list[dict]] = {}
        base = self._base_url()
        for start in range(0, len(ids), 200):
            chunk = ", ".join(f"'{_lit(i)}'" for i in ids[start:start + 200])
            link = self.fields.get("link")
            extra = f"{link}, " if link else ""
            rows = self._records(f"SELECT Id, CaseNumber, Status, {extra}RecordType.Name, ParentId "
                                 f"FROM Case WHERE ParentId IN ({chunk})")
            for r in rows:
                out.setdefault(r["ParentId"], []).append({
                    "id": r["Id"], "case_number": r.get("CaseNumber"), "status": r.get("Status"),
                    "links": parse(_dig(r, link)) if link else [],
                    "record_type": (r.get("RecordType") or {}).get("Name"),
                    "url": f"{base}/lightning/r/Case/{r['Id']}/view" if base else None})
        return out

    def latest_emails(self, ids: list[str], per_case: int = 5) -> dict[str, list[dict]]:
        """The newest emails on each case, for reading follow-ups on DSP cases."""
        out: dict[str, list[dict]] = {}
        for start in range(0, len(ids), 200):
            chunk = ", ".join(f"'{_lit(i)}'" for i in ids[start:start + 200])
            rows = self._records("SELECT ParentId, FromAddress, Subject, TextBody, MessageDate, "
                                 f"Incoming FROM EmailMessage WHERE ParentId IN ({chunk}) "
                                 "ORDER BY MessageDate DESC LIMIT 2000")
            for r in rows:
                held = out.setdefault(r["ParentId"], [])
                if len(held) < per_case:
                    held.append({"who": r.get("FromAddress"), "ours": not r.get("Incoming"),
                                 "at": r.get("MessageDate"),
                                 "text": clean(f"{r.get('Subject') or ''}\n"
                                               f"{r.get('TextBody') or ''}")})
        return out

    def brand_stats(self, brands: list[str]) -> dict[str, dict]:
        field = self.fields.get("brand")
        if not field or not brands:
            return {}
        names = ", ".join(f"'{_lit(b)}'" for b in brands[:200])
        stats: dict[str, dict] = {}
        for key, where in (("open", "IsClosed = false"),
                           ("last_30_days", "CreatedDate = LAST_N_DAYS:30")):
            rows = self._records(f"SELECT {field} b, COUNT(Id) n FROM Case "
                                 f"WHERE {field} IN ({names}) AND {where} GROUP BY {field}")
            for r in rows:
                stats.setdefault(r["b"], {})[key] = r["n"]
        return stats

    def timeline(self, case_id: str) -> dict[str, list[dict]]:
        cid = _lit(case_id)
        emails = self._records(
            "SELECT FromAddress, ToAddress, Subject, TextBody, MessageDate, Incoming "
            f"FROM EmailMessage WHERE ParentId = '{cid}' ORDER BY MessageDate")
        comments = self._records("SELECT CommentBody, CreatedDate, CreatedBy.Name "
                                 f"FROM CaseComment WHERE ParentId = '{cid}' ORDER BY CreatedDate")
        history = self._records("SELECT Field, OldValue, NewValue, CreatedDate, CreatedBy.Name "
                                f"FROM CaseHistory WHERE CaseId = '{cid}' ORDER BY CreatedDate")
        return {
            "emails": [{"from": e.get("FromAddress"), "to": e.get("ToAddress"),
                        "subject": clean(e.get("Subject")), "text": clean(e.get("TextBody")),
                        "at": e.get("MessageDate"), "incoming": bool(e.get("Incoming"))}
                       for e in emails],
            "comments": [{"text": clean(c.get("CommentBody")), "at": c.get("CreatedDate"),
                          "by": (c.get("CreatedBy") or {}).get("Name")} for c in comments],
            "history": [{"field": h.get("Field"), "old": h.get("OldValue"),
                         "new": h.get("NewValue"), "at": h.get("CreatedDate"),
                         "by": (h.get("CreatedBy") or {}).get("Name")} for h in history],
        }

    def resolved_since(self, since_iso: str, limit: int = 2000) -> list[dict]:
        f = self.fields
        cols = ["Id", "CaseNumber", "Subject", "CreatedDate", "ClosedDate"]
        cols += [c for c in (f.get("module"), f.get("sub_module"), f.get("third_party")) if c]
        rows = self._records(
            f"SELECT {', '.join(dict.fromkeys(cols))}, "
            "(SELECT CommentBody FROM CaseComments ORDER BY CreatedDate DESC LIMIT 1) "
            f"FROM Case WHERE IsClosed = true AND ClosedDate >= {since_iso} "
            f"ORDER BY ClosedDate DESC LIMIT {int(limit)}")
        base = self._base_url()
        out = []
        for r in rows:
            comments = (r.get("CaseComments") or {}).get("records") or []
            third = _dig(r, f["third_party"]) if f.get("third_party") else None
            out.append({
                "id": r["Id"], "case_number": r.get("CaseNumber"),
                "subject": clean(r.get("Subject")),
                "module": _dig(r, f["module"]) if f.get("module") else None,
                "sub_module": _dig(r, f["sub_module"]) if f.get("sub_module") else None,
                "third_parties": [x for x in (third or "").split(";") if x],
                "opened_at": r.get("CreatedDate"), "closed_at": r.get("ClosedDate"),
                "resolution_note": clean(comments[0].get("CommentBody")) if comments else None,
                "url": f"{base}/lightning/r/Case/{r['Id']}/view" if base else None})
        return out
