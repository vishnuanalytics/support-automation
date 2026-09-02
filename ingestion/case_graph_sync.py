"""
KIL-a — sync the Salesforce Case *lifecycle* into the Neo4j case-history graph.

`case_memory_sync` stores only the accepted **resolution** of a closed Case
(one embedded row + a `(:Reply)` node). This walks the whole lifecycle: one
`(:Case)` node per Case (any status) and one `(:Message)` node per turn —
inbound emails, agent replies, public comments, Chatter posts, plus the
original Case description — so the Knowledge Integrity Loop can judge new text
against what was actually said on similar cases, not only KB docs.

    python -m ingestion.case_graph_sync --backfill              # every Case
    python -m ingestion.case_graph_sync --backfill --limit 50
    python -m ingestion.case_graph_sync --since 2026-08-01       # LastModifiedDate >=
    python -m ingestion.case_graph_sync --case 500XX0000000abc   # one Case
    python -m ingestion.case_graph_sync --dry-run

Resumable: a `graph_sync_state` row (`case_graph:<tenant>`) holds the
LastModifiedDate high-water mark + counters. Re-running is idempotent (MERGE on
`sf_id` / message `id`). Neo4j / Salesforce unreachable -> logs and exits 0.

The vector side (embeddings in `case_memory` for RAG) stays with
`ingestion.case_memory_sync --from-salesforce`; run both in a backfill.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402
from interpreter import case_memory  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("case_graph_sync")

_TENANT = os.environ.get("DEFAULT_TENANT_ID", "00000000-0000-0000-0000-000000000000")
_TEXT_LIMIT = int(os.environ.get("CASE_GRAPH_TEXT_LIMIT", "6000"))

# a CaseComment the pipeline left for review is the bot's proposed reply, not a
# human note — KIL-b's judge treats the two very differently.
_BOT_DRAFT_MARKERS = ("[bot draft", "[draft", "suggested draft", "review before sending")

# child sub-queries kept small + separately guarded — a Feed relationship that
# isn't queryable in a given org shouldn't sink the whole pull.
_CASE_SOQL = (
    "SELECT Id, CaseNumber, Subject, Description, Status, Type, Reason, Priority, "
    "Origin, IsClosed, CreatedDate, ClosedDate, LastModifiedDate, OwnerId, AccountId, "
    "ContactId, Contact.Email, Module__c, Routed_Team__c, Account.Tier__c, "
    "(SELECT Id, CommentBody, CreatedById, CreatedDate, IsPublished FROM CaseComments), "
    "(SELECT Id, Incoming, FromAddress, ToAddress, TextBody, MessageDate, CreatedById "
    " FROM EmailMessages) "
    "FROM Case"
)


# ── message extraction ────────────────────────────────────────────────────
def _msg(mid, role, author_kind, author_id, ts, text) -> dict | None:
    body = case_memory.redact(text or "", limit=_TEXT_LIMIT)
    if not body.strip():
        return None
    return {"id": str(mid), "role": role, "author_kind": author_kind,
            "author_id": author_id or None, "ts": ts, "text": body}


def _messages(case: dict) -> list[dict]:
    """Every turn on the Case, oldest first, as (:Message) rows."""
    out: list[dict] = []
    contact_email = ((case.get("Contact") or {}).get("Email") or "").lower()

    if case.get("Description"):
        out.append(_msg(f"{case['Id']}:desc", "inbound", "customer", case.get("ContactId"),
                        case.get("CreatedDate"), case["Description"]))

    for em in ((case.get("EmailMessages") or {}).get("records") or []):
        incoming = bool(em.get("Incoming"))
        frm = (em.get("FromAddress") or "").lower()
        kind = "customer" if (incoming or (contact_email and frm == contact_email)) else "agent"
        out.append(_msg(em["Id"], "inbound" if kind == "customer" else "agent_reply",
                        kind, em.get("CreatedById"), em.get("MessageDate"), em.get("TextBody")))

    for cc in ((case.get("CaseComments") or {}).get("records") or []):
        low = (cc.get("CommentBody") or "").lower().lstrip()
        is_draft = any(low.startswith(m) or m in low[:60] for m in _BOT_DRAFT_MARKERS)
        out.append(_msg(cc["Id"], "draft" if is_draft else "agent_note",
                        "bot" if is_draft else "agent", cc.get("CreatedById"),
                        cc.get("CreatedDate"), cc.get("CommentBody")))

    for fi in ((case.get("Feeds") or {}).get("records") or []):
        out.append(_msg(fi["Id"], "chatter", "agent", fi.get("CreatedById"),
                        fi.get("CreatedDate"), fi.get("Body")))

    out = [m for m in out if m]
    out.sort(key=lambda m: m.get("ts") or "")
    return out


def _case_row(case: dict) -> dict:
    return {
        "sf_id": case["Id"],
        "case_number": case.get("CaseNumber"),
        "subject": case.get("Subject"),
        "tenant_id": _TENANT,
        "status": case.get("Status"),
        "is_closed": bool(case.get("IsClosed")),
        "tier": (case.get("Account") or {}).get("Tier__c"),
        "routed_team": case.get("Routed_Team__c"),
        "origin": case.get("Origin"),
        "opened_at": case.get("CreatedDate"),
        "closed_at": case.get("ClosedDate"),
        "module": case.get("Module__c"),
        "case_type": case.get("Type"),
        "account_id": case.get("AccountId"),
    }


# ── Salesforce pull ──────────────────────────────────────────────────────
def _fetch(sf, *, since: str | None, limit: int, one_id: str | None) -> list[dict]:
    from interpreter import salesforce
    where = []
    if one_id:
        where.append(f"Id = '{salesforce._soql_lit(one_id)}'")
    if since:
        lit = since if "T" in since else f"{since}T00:00:00Z"
        where.append(f"LastModifiedDate >= {salesforce._soql_lit(lit)}")
    soql = _CASE_SOQL + (f" WHERE {' AND '.join(where)}" if where else "")
    soql += f" ORDER BY LastModifiedDate ASC LIMIT {int(limit)}"
    try:
        return sf.query(soql).get("records", [])
    except Exception as e:  # noqa: BLE001
        # a `Feeds` sub-query can be rejected in some orgs — retry without it
        if "Feeds" in soql:
            log.warning("Case SOQL failed (%s) — retrying without Chatter feed", e)
            return _fetch_no_feed(sf, since=since, limit=limit, one_id=one_id)
        raise


def _fetch_no_feed(sf, *, since, limit, one_id):
    from interpreter import salesforce
    where = []
    if one_id:
        where.append(f"Id = '{salesforce._soql_lit(one_id)}'")
    if since:
        lit = since if "T" in since else f"{since}T00:00:00Z"
        where.append(f"LastModifiedDate >= {salesforce._soql_lit(lit)}")
    soql = _CASE_SOQL + (f" WHERE {' AND '.join(where)}" if where else "")
    soql += f" ORDER BY LastModifiedDate ASC LIMIT {int(limit)}"
    return sf.query(soql).get("records", [])


# ── state ────────────────────────────────────────────────────────────────
def _load_state(sb, scope: str) -> dict:
    try:
        rows = (sb.table("graph_sync_state").select("*").eq("scope", scope)
                .limit(1).execute().data or [])
        return rows[0] if rows else {}
    except Exception as e:  # noqa: BLE001
        log.warning("graph_sync_state read: %s", e)
        return {}


def _save_state(sb, scope: str, *, last_modified: str | None, cases: int, messages: int) -> None:
    try:
        prev = _load_state(sb, scope)
        sb.table("graph_sync_state").upsert({
            "scope": scope,
            "tenant_id": _TENANT,
            "last_modified": last_modified or prev.get("last_modified"),
            "cases_synced": (prev.get("cases_synced") or 0) + cases,
            "messages_synced": (prev.get("messages_synced") or 0) + messages,
            "last_run_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="scope").execute()
    except Exception as e:  # noqa: BLE001
        log.warning("graph_sync_state write: %s", e)


# ── run ──────────────────────────────────────────────────────────────────
def sync(*, since: str | None, limit: int, one_id: str | None, dry: bool) -> int:
    from interpreter import salesforce
    if not salesforce.available():
        log.warning("no Salesforce creds — nothing to sync")
        return 0
    sb = None
    scope = f"case_graph:{_TENANT}"
    if not dry:
        try:
            from ingestion.neo4j_sync import ensure_constraints, get_neo4j_driver
            _d = get_neo4j_driver()
            ensure_constraints(_d)
            _d.close()
        except Exception as e:  # noqa: BLE001
            log.warning("constraint ensure skipped: %s", e)
        sb = get_supabase()
        if since is None and not one_id:
            since = (_load_state(sb, scope).get("last_modified") or None)
            if since:
                log.info("resuming from LastModifiedDate >= %s", since)

    sf = salesforce.client_for(None)
    cases = _fetch(sf, since=since, limit=limit, one_id=one_id)
    log.info("%d Case(s) to sync", len(cases))

    n_cases = n_msgs = 0
    high_water = since
    for case in cases:
        row = _case_row(case)
        msgs = _messages(case)
        high_water = max(high_water or "", case.get("LastModifiedDate") or "") or None
        if dry:
            log.info("[dry-run] %s %s  status=%s  messages=%d",
                     row["case_number"], row["sf_id"], row["status"], len(msgs))
            n_cases += 1
            n_msgs += len(msgs)
            continue
        if case_memory.sync_case_lifecycle(row, msgs):
            n_cases += 1
            n_msgs += len(msgs)
        else:
            log.warning("graph MERGE failed for %s — stopping", row["sf_id"])
            break

    if not dry and n_cases:
        _save_state(sb, scope, last_modified=high_water, cases=n_cases, messages=n_msgs)
    log.info("%s %d Case(s) / %d Message(s)",
             "would sync" if dry else "synced", n_cases, n_msgs)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingestion.case_graph_sync")
    ap.add_argument("--backfill", action="store_true",
                    help="ignore the saved checkpoint; walk from the start")
    ap.add_argument("--since", default=None, help="ISO date/datetime; LastModifiedDate >=")
    ap.add_argument("--case", dest="one_id", default=None, help="sync a single Case Id")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    since = "1970-01-01" if args.backfill else args.since
    return sync(since=since, limit=args.limit, one_id=args.one_id, dry=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
