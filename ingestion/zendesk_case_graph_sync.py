"""
Zendesk case-lifecycle graph sync (Phase 31 chunk 3) — the Zendesk
equivalent of `ingestion/case_graph_sync.py`, which is Salesforce-only.

For every tenant with `case_connector = 'zendesk'` + an active `zendesk`
integration: walk tickets updated since the checkpoint (Zendesk's
**Incremental Export** API), and MERGE one `(:Case)` + one `(:Message)`
per ticket comment into Neo4j via the same connector-agnostic
`interpreter.case_memory.sync_case_lifecycle` the Salesforce sync uses —
so a Zendesk tenant gets the identical case graph: `(:Case)-[:HAS_MESSAGE]
->(:Message)`, `-[:ABOUT]->(:Module)` / `-[:OF_TYPE]->(:CaseType)` /
`-[:FOR_ACCOUNT]->(:Account)`, and (Phase 30) `-[:FILED_BY]->(:Contact)`
+ `(:Contact)-[:AT_ACCOUNT]->(:Account)` + `Account.domain`.

    python -m ingestion.zendesk_case_graph_sync --once
    python -m ingestion.zendesk_case_graph_sync --tenant <uuid> --backfill
    python -m ingestion.zendesk_case_graph_sync --once --dry-run

Resumable per tenant: a `graph_sync_state` row keyed
`case_graph:zendesk:<tenant>` holds the `updated_at` high-water mark.
Idempotent (MERGE on the ticket id + comment id). Best-effort: no creds /
Zendesk or Neo4j down -> logs + exits 0.

Where Zendesk's model doesn't map onto Salesforce's, this follows the
connector's own choices (see interpreter/zendesk.py's docstring): `type`
(question/incident/problem/task) -> `case_type`, no `module`; `group` name
-> `routed_team`; `organization` -> `Account`; no first-class `closed_at`
so `updated_at` stands in once a ticket is solved/closed.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from interpreter import case_memory, zendesk  # noqa: E402
from ingestion.case_graph_sync import _BOT_DRAFT_MARKERS  # noqa: E402  keep in sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zendesk_case_graph_sync")

_TEXT_LIMIT = int(os.environ.get("CASE_GRAPH_TEXT_LIMIT", "6000"))
_CKPT_EVERY = 50
_SOLVED = ("solved", "closed")


def _sb():
    from ingestion.scraper import get_supabase
    return get_supabase()


def _iso_to_unix(iso: str | None) -> int:
    if not iso:
        return 0
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


# ── state ────────────────────────────────────────────────────────────────
def _load_state(sb, scope: str) -> dict:
    try:
        rows = (sb.table("graph_sync_state").select("*").eq("scope", scope)
                .limit(1).execute().data or [])
        return rows[0] if rows else {}
    except Exception as e:  # noqa: BLE001
        log.warning("graph_sync_state read: %s", e)
        return {}


def _save_state(sb, scope: str, tenant_id: str, *, high_water: str | None,
                cases: int, messages: int) -> None:
    try:
        prev = _load_state(sb, scope)
        now = datetime.now(timezone.utc).isoformat()
        sb.table("graph_sync_state").upsert({
            "scope": scope,
            "tenant_id": tenant_id,
            "last_modified": high_water or prev.get("last_modified"),
            "cases_synced": (prev.get("cases_synced") or 0) + cases,
            "messages_synced": (prev.get("messages_synced") or 0) + messages,
            "last_run_at": now,
            "updated_at": now,
        }, on_conflict="scope").execute()
    except Exception as e:  # noqa: BLE001
        log.warning("graph_sync_state write: %s", e)


# ── Zendesk pull ────────────────────────────────────────────────────────
def _incremental_tickets(zc, start_unix: int, limit: int) -> list[dict]:
    """Zendesk Incremental Export — tickets with `updated_at >= start_unix`,
    oldest first, paged via `end_time` until `end_of_stream` (or `limit`)."""
    out: list[dict] = []
    start = max(start_unix, 0)
    for _ in range(50):                       # hard page cap
        page = zc.request("GET", "/incremental/tickets.json",
                          params={"start_time": start, "per_page": 1000})
        out.extend(page.get("tickets") or [])
        if page.get("end_of_stream") or not page.get("end_time") \
                or page.get("end_time") == start or len(out) >= limit:
            break
        start = page["end_time"]
    return out[:limit]


class _Cache:
    """Per-run lookups so a repeated requester / org isn't re-fetched."""

    def __init__(self, zc):
        self.zc = zc
        self.users: dict[int, dict] = {}
        self.orgs: dict[int, dict] = {}
        self.groups: dict[int, str] | None = None

    def user(self, uid) -> dict:
        if uid is None:
            return {}
        if uid not in self.users:
            try:
                self.users[uid] = (self.zc.request("GET", f"/users/{uid}.json")
                                   .get("user") or {})
            except Exception as e:  # noqa: BLE001
                log.warning("zendesk user %s: %s", uid, e)
                self.users[uid] = {}
        return self.users[uid]

    def org(self, oid) -> dict:
        if oid is None:
            return {}
        if oid not in self.orgs:
            try:
                self.orgs[oid] = (self.zc.request("GET", f"/organizations/{oid}.json")
                                  .get("organization") or {})
            except Exception as e:  # noqa: BLE001
                log.warning("zendesk org %s: %s", oid, e)
                self.orgs[oid] = {}
        return self.orgs[oid]

    def group_name(self, gid) -> str | None:
        if gid is None:
            return None
        if self.groups is None:
            try:
                self.groups = {g["id"]: g.get("name")
                               for g in (self.zc.request("GET", "/groups.json")
                                         .get("groups") or [])}
            except Exception as e:  # noqa: BLE001
                log.warning("zendesk groups: %s", e)
                self.groups = {}
        return self.groups.get(gid)


def _msg(mid, role, author_kind, author_id, ts, text) -> dict | None:
    body = case_memory.redact(text or "", limit=_TEXT_LIMIT)
    if not body.strip():
        return None
    return {"id": str(mid), "role": role, "author_kind": author_kind,
            "author_id": str(author_id) if author_id is not None else None,
            "ts": ts, "text": body}


def _ticket_messages(zc, ticket: dict, cache: _Cache) -> list[dict]:
    """Every comment on the ticket, oldest first, as (:Message) rows.
    Zendesk's comment thread *is* the whole conversation (no separate
    EmailMessage object), so the first comment == the ticket description."""
    tid = ticket["id"]
    requester_id = ticket.get("requester_id")
    try:
        comments = (zc.request("GET", f"/tickets/{tid}/comments.json")
                    .get("comments") or [])
    except Exception as e:  # noqa: BLE001
        log.warning("zendesk ticket %s comments: %s", tid, e)
        comments = []

    out: list[dict] = []
    for c in comments:
        aid = c.get("author_id")
        is_requester = aid is not None and aid == requester_id
        role_of_user = (cache.user(aid).get("role") or "") if not is_requester else "end-user"
        is_customer = is_requester or role_of_user == "end-user"
        public = bool(c.get("public", True))
        low = (c.get("body") or c.get("plain_body") or "").lower().lstrip()
        is_draft = (not public) and any(low.startswith(m) or m in low[:60]
                                        for m in _BOT_DRAFT_MARKERS)
        if is_draft:
            role, kind = "draft", "bot"
        elif is_customer:
            role, kind = "inbound", "customer"
        elif public:
            role, kind = "agent_reply", "agent"
        else:
            role, kind = "agent_note", "agent"
        m = _msg(c.get("id"), role, kind, aid, c.get("created_at"),
                 c.get("body") or c.get("plain_body"))
        if m:
            out.append(m)
    out.sort(key=lambda m: m.get("ts") or "")
    return out


def _ticket_row(ticket: dict, tenant_id: str, cache: _Cache) -> dict:
    status = (ticket.get("status") or "").lower()
    requester = cache.user(ticket.get("requester_id"))
    org = cache.org(ticket.get("organization_id"))
    domains = org.get("domain_names") or []
    return {
        "sf_id": str(ticket["id"]),
        "case_number": str(ticket["id"]),
        "subject": ticket.get("subject"),
        "tenant_id": tenant_id,
        "status": ticket.get("status"),
        "is_closed": status in _SOLVED,
        "tier": None,
        "routed_team": cache.group_name(ticket.get("group_id")),
        "origin": (ticket.get("via") or {}).get("channel"),
        "opened_at": ticket.get("created_at"),
        "closed_at": ticket.get("updated_at") if status in _SOLVED else None,
        "module": None,                       # Zendesk has no product-area picklist
        "case_type": ticket.get("type"),      # question / incident / problem / task
        "account_id": (str(ticket["organization_id"])
                       if ticket.get("organization_id") else None),
        "account_domain": (domains[0].strip().lower() or None) if domains else None,
        "contact_email": (requester.get("email") or "").strip().lower() or None,
    }


# ── run ────────────────────────────────────────────────────────────────
def _sync_tenant(sb, tenant_id: str, *, since: str | None, limit: int, dry: bool) -> tuple[int, int]:
    zc = zendesk._client(tenant_id, sb)
    if zc is None:
        log.info("tenant %s: zendesk not configured — skipped", tenant_id)
        return 0, 0

    scope = f"case_graph:zendesk:{tenant_id}"
    start_iso = since if since is not None else (_load_state(sb, scope).get("last_modified"))
    start_unix = _iso_to_unix(start_iso)
    log.info("tenant %s: incremental tickets since %s (unix %d)",
             tenant_id, start_iso or "the beginning", start_unix)

    tickets = _incremental_tickets(zc, start_unix, limit)
    log.info("tenant %s: %d ticket(s) to sync", tenant_id, len(tickets))
    cache = _Cache(zc)
    n_cases = n_msgs = ck_c = ck_m = 0
    high_water = start_iso

    def _ckpt():
        nonlocal ck_c, ck_m
        if not dry and ck_c:
            _save_state(sb, scope, tenant_id, high_water=high_water, cases=ck_c, messages=ck_m)
            ck_c = ck_m = 0

    for t in tickets:
        row = _ticket_row(t, tenant_id, cache)
        msgs = _ticket_messages(zc, t, cache)
        high_water = max(high_water or "", t.get("updated_at") or "") or None
        if dry:
            log.info("[dry-run] ticket %s status=%s messages=%d",
                     row["sf_id"], row["status"], len(msgs))
            n_cases += 1
            n_msgs += len(msgs)
            continue
        if case_memory.sync_case_lifecycle(row, msgs):
            n_cases += 1
            n_msgs += len(msgs)
            ck_c += 1
            ck_m += len(msgs)
        else:
            log.warning("tenant %s: graph MERGE failed for ticket %s — stopping",
                        tenant_id, row["sf_id"])
            break
        if ck_c >= _CKPT_EVERY:
            _ckpt()
    _ckpt()
    return n_cases, n_msgs


def sync(*, tenant_id: str | None = None, since: str | None = None,
         limit: int = 2000, dry: bool = False) -> int:
    sb = _sb()
    tenants = zendesk.active_connector_tenants(sb, tenant_id)
    if not tenants:
        log.info("no active zendesk-connector tenants — nothing to sync")
        return 0

    if not dry:
        try:
            from ingestion.neo4j_sync import ensure_constraints, get_neo4j_driver
            ensure_constraints(get_neo4j_driver())
        except Exception as e:  # noqa: BLE001
            log.warning("constraint ensure skipped: %s", e)

    tot_c = tot_m = 0
    for tid in tenants:
        try:
            c, m = _sync_tenant(sb, tid, since=since, limit=limit, dry=dry)
        except Exception as e:  # noqa: BLE001
            log.warning("tenant %s: %s", tid, e)
            c = m = 0
        tot_c += c
        tot_m += m
    log.info("%s %d ticket(s) / %d message(s) across %d tenant(s)",
             "would sync" if dry else "synced", tot_c, tot_m, len(tenants))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingestion.zendesk_case_graph_sync")
    ap.add_argument("--tenant", help="only this tenant id")
    ap.add_argument("--since", default=None, help="ISO date/datetime; updated_at >=")
    ap.add_argument("--backfill", action="store_true",
                    help="ignore the saved checkpoint; walk from the start")
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    since = "1970-01-01T00:00:00Z" if args.backfill else args.since
    return sync(tenant_id=args.tenant, since=since, limit=args.limit, dry=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
