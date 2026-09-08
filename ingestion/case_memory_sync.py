"""
Phase 21 — populate `case_memory` from resolved Cases.

Source 1 (default): `runs` rows where a human sent a reply — `human_reply`
set, or `human_action` in {sent, edited, guided_resume}. That is the
accepted-resolution signal the Phase 11 / 20m loop already records.

Source 2 (`--from-salesforce`): closed Salesforce Cases + their last outbound
EmailMessage — Cases resolved outside the run loop.

Each Case -> one `case_memory` row (redacted summary + resolution text + a
384-d embedding), upserted to Supabase, then MERGE'd into Neo4j with its
SIMILAR_TO edges (best-effort).

    python -m ingestion.case_memory_sync --once            # last 90 days from runs
    python -m ingestion.case_memory_sync --since 2026-08-01
    python -m ingestion.case_memory_sync --from-salesforce --once
    python -m ingestion.case_memory_sync --dry-run

Idempotent (upsert on case_sf_id). Add to .github/workflows/daily-sync.yml
next to neo4j_sync.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402
from interpreter import case_memory  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("case_memory_sync")

# only a genuine human-accepted resolution — not a "needs review" CaseComment.
_RESOLVED_ACTIONS = ("sent", "sent_as_is", "edited", "edit", "rewrote", "guided_resume")
_NOT_A_RESOLUTION = ("[bot draft", "[draft", "suggested draft", "review before sending")


def _row_from_run(r: dict) -> dict | None:
    case_id = r.get("case_id")
    if not case_id:
        return None
    action = (r.get("human_action") or "").lower()
    if action not in _RESOLVED_ACTIONS:
        return None
    reply = (r.get("human_reply") or "").strip()
    from_bot = False
    if not reply and action in ("sent", "sent_as_is"):
        reply = (r.get("draft") or "").strip()
        from_bot = True
    if not reply:
        return None
    low = reply.lower()
    if any(low.startswith(p) or p in low[:60] for p in _NOT_A_RESOLUTION):
        return None                     # the bot's own unreviewed draft, not a resolution

    written = (r.get("sf_writeback") or {}).get("written") or {}
    payload = r.get("case_payload") or {}
    body = payload.get("body") or payload.get("description") or r.get("subject") or ""
    kind = case_memory.classify_resolution_kind(
        r.get("human_action"), reply, from_bot=from_bot)
    cid = str(case_id)
    return {
        "case_sf_id": cid,
        # `runs.case_id` (-> `case_sf_id` above) is the Case *Number* for
        # this source (e.g. "00001130"), not the real Salesforce Id --
        # `case_payload["sf_id"]` (registry.py's `case.get("sf_id") or
        # case.get("id")` convention, used everywhere else) is the actual
        # Id. `case_sf_id` is the MERGE/upsert identity key for already-live
        # case_memory rows and Neo4j Case nodes, so it can't be repointed at
        # the real Id without orphaning/duplicating existing data -- this
        # separate field exists purely so `_enrich_from_sf` can query
        # Salesforce by the *real* Id instead of searching `Id` for a case
        # number, which could never match. Root cause of the
        # DUPLICATE_OF-never-fires bug, found while re-syncing after the
        # earlier account_id/`want`-filter fix still produced zero new
        # edges. `_from_salesforce`'s rows need no equivalent -- their
        # `case_sf_id` is already the real Id.
        "_sf_lookup_id": payload.get("sf_id"),
        "tenant_id": r["tenant_id"],
        "case_number": payload.get("case_number") or (cid if cid.isdigit() else None),
        "subject": r.get("subject") or payload.get("subject"),
        "body_summary": body,
        "case_type": written.get("Type"),
        "module": written.get("Module__c"),
        "submodule": written.get("SubModule__c"),
        "region": r.get("region") or written.get("Region__c"),
        "tier": r.get("tier"),
        "resolution_kind": kind,
        "resolution_text": reply,
        "generalizable": not case_memory.looks_specific(reply),
        "agent_user_id": None,
        "resolved_at": r.get("feedback_checked_at") or r.get("created_at"),
        "source": "runs",
    }


def _iter_runs(sb, since_iso: str, limit: int):
    q = (sb.table("runs")
         .select("case_id,tenant_id,subject,tier,region,sf_writeback,"
                 "case_payload,human_reply,human_action,draft,created_at,feedback_checked_at")
         .gte("created_at", since_iso)
         .order("created_at", desc=True)
         .limit(limit))
    return q.execute().data or []


def _enrich_from_sf(rows: list[dict]) -> None:
    """Fill case_number / case_type / module / tier / account_id from
    Salesforce for rows whose `case_sf_id` is a real Id (the `runs` backfill
    often lacks them).

    The `want` filter used to check only `case_type` — meaning a row that
    already had `case_type` (from `sf_writeback`, or from `_from_salesforce`'s
    own direct SOQL) was treated as "already enriched" and skipped entirely,
    silently starving it of `account_id` even though that field was never
    set anywhere else. Root cause of a real bug (found while investigating
    why Neo4j's DUPLICATE_OF edges never fire): `same_account` in
    `_sync_rows` needs a non-empty `account_id` on both sides, so every row
    that hit this path came out with `account_id` unset -> DUPLICATE_OF's
    same-account gate was always False. Checking `account_id` too closes it."""
    from interpreter import salesforce

    def _lookup_id(r: dict) -> str | None:
        # prefer the real Id (`_sf_lookup_id`, set by `_row_from_run` from
        # `case_payload["sf_id"]`) over `case_sf_id`, which for that source
        # is actually the Case Number -- see `_row_from_run`'s comment.
        # `_from_salesforce`'s rows have no `_sf_lookup_id`; their
        # `case_sf_id` is already the real Id.
        v = r.get("_sf_lookup_id") or r["case_sf_id"]
        return v if isinstance(v, str) and len(v) in (15, 18) else None

    if not salesforce.available():
        return
    want = {lid for r in rows
            if (lid := _lookup_id(r)) and (not r.get("case_type") or not r.get("account_id"))}
    if not want:
        return
    try:
        sf = salesforce.client_for(None)
        ids = ", ".join(f"'{salesforce._soql_lit(i)}'" for i in list(want)[:200])
        recs = sf.query(
            "SELECT Id, CaseNumber, Type, Module__c, Region__c, AccountId, "
            "Account.Tier__c, IsClosed, ClosedDate "
            f"FROM Case WHERE Id IN ({ids})"
        ).get("records", [])
        by_id = {c["Id"]: c for c in recs}
    except Exception as e:  # noqa: BLE001
        log.warning("SF enrich failed: %s", e)
        return
    for r in rows:
        c = by_id.get(_lookup_id(r))
        if not c:
            continue
        r["case_number"] = r.get("case_number") or c.get("CaseNumber")
        r["case_type"] = r.get("case_type") or c.get("Type")
        r["module"] = r.get("module") or c.get("Module__c")
        r["region"] = r.get("region") or c.get("Region__c")
        r["tier"] = r.get("tier") or (c.get("Account") or {}).get("Tier__c")
        r["account_id"] = r.get("account_id") or c.get("AccountId")
        # audit WF-6 — a Case that was closed and is open again means the
        # resolution didn't hold; drop it from the citable set.
        if c.get("ClosedDate") and c.get("IsClosed") is False:
            r["resolution_kind"] = "reopened"
            r["generalizable"] = False


def _sync_rows(rows: list[dict], *, dry: bool) -> int:
    sb = get_supabase()
    done = 0
    if not dry:
        _enrich_from_sf(rows)
    # de-dupe by case: keep the newest resolution per case_sf_id
    by_case: dict[str, dict] = {}
    for row in rows:
        key = row["case_sf_id"]
        if key not in by_case:
            by_case[key] = row
    for row in by_case.values():
        text = f"{row.get('subject') or ''}\n{case_memory.summarize(row.get('body_summary'))}"
        if dry:
            log.info("[dry-run] %s  kind=%s generalizable=%s  %r",
                     row["case_sf_id"], row["resolution_kind"],
                     row["generalizable"], (row["resolution_text"] or "")[:80])
            done += 1
            continue
        try:
            row["embedding"] = case_memory.embed(text)
        except Exception as e:  # noqa: BLE001
            log.warning("embed failed for %s: %s — storing without vector", row["case_sf_id"], e)
            row["embedding"] = None
        case_memory.upsert(sb, row)
        # SIMILAR_TO edges: kNN this case against the rest of the tenant's memory
        similar = []
        if row["embedding"]:
            try:
                hits = sb.rpc("match_case_memory", {
                    "query_embedding": row["embedding"],
                    "p_tenant": str(row["tenant_id"]), "match_count": 8,
                }).execute().data or []
                similar = [h for h in hits if h["case_sf_id"] != row["case_sf_id"]][:6]
                # tag same-account neighbours so sync_graph can MERGE
                # DUPLICATE_OF for the near-identical ones (audit NEO-3)
                acc = (row.get("account_id") or "")
                for h in similar:
                    h["same_account"] = bool(acc) and h.get("account_id") == acc
            except Exception as e:  # noqa: BLE001
                log.warning("similar lookup for %s: %s", row["case_sf_id"], e)
        case_memory.sync_graph(row, similar)
        done += 1
    return done


def _from_salesforce(since_iso: str, limit: int, *, until_iso: str | None = None,
                     tenant_id: str | None = None) -> list[dict]:
    from interpreter import salesforce
    # `client_for` resolves the tenant's own connected org (Vault), falling
    # back to the env client; only bail when even that has nothing.
    sf = salesforce._try_client(tenant_id)
    if sf is None:
        log.warning("--from-salesforce: no Salesforce client for tenant %s", tenant_id)
        return []
    tid = tenant_id or "00000000-0000-0000-0000-000000000000"
    where = [f"IsClosed = true AND ClosedDate >= {salesforce._soql_lit(since_iso)}"]
    if until_iso:
        where.append(f"ClosedDate < {salesforce._soql_lit(until_iso)}")
    cases = sf.query(
        "SELECT Id, CaseNumber, Subject, Description, Type, Module__c, Region__c, "
        "AccountId, ClosedDate, Account.Tier__c FROM Case "
        f"WHERE {' AND '.join(where)} ORDER BY ClosedDate DESC "
        f"LIMIT {int(limit)}"
    ).get("records", [])
    from interpreter import case_import
    out = []
    for c in cases:
        em = sf.query(
            "SELECT TextBody FROM EmailMessage WHERE ParentId = "
            f"'{salesforce._soql_lit(c['Id'])}' AND Incoming = false "
            "ORDER BY MessageDate DESC LIMIT 5"
        ).get("records", [])
        # the *last* outbound is very often "we're following up on Case ID…"
        # boilerplate, not the fix — clean + boilerplate-filter + mine the
        # quoted history for the substantive answer (same as the importer).
        reply = case_import.best_resolution([m.get("TextBody") or "" for m in em])
        if not reply:
            continue
        out.append({
            "case_sf_id": c["Id"], "tenant_id": tid,
            "case_number": c.get("CaseNumber"), "subject": c.get("Subject"),
            "body_summary": c.get("Description") or c.get("Subject") or "",
            "case_type": c.get("Type"), "module": c.get("Module__c"),
            "region": c.get("Region__c"), "account_id": c.get("AccountId"),
            "tier": (c.get("Account") or {}).get("Tier__c"),
            "resolution_kind": case_memory.classify_resolution_kind(None, reply),
            "resolution_text": reply,
            "generalizable": not case_memory.looks_specific(reply),
            "agent_user_id": None, "resolved_at": c.get("ClosedDate"), "source": "salesforce",
        })
    return out


def _from_zendesk(since_iso: str, limit: int) -> list[dict]:
    """Phase 31 chunk 4 — resolved Zendesk tickets, for every
    `case_connector=zendesk` tenant. The resolution text is the **last
    public non-requester comment** on a solved/closed ticket. Reuses the
    ticket-graph sync's Incremental Export + per-run cache helpers."""
    from interpreter import case_memory, zendesk
    from ingestion.zendesk_case_graph_sync import _Cache, _incremental_tickets, _iso_to_unix

    sb = get_supabase()
    start_unix = _iso_to_unix(since_iso)
    out: list[dict] = []
    for tid in zendesk.active_connector_tenants(sb):
        zc = zendesk._client(tid, sb)
        if zc is None:
            continue
        cache = _Cache(zc)
        for t in _incremental_tickets(zc, start_unix, limit):
            if (t.get("status") or "").lower() not in ("solved", "closed"):
                continue
            reply, agent_id = _zendesk_resolution(zc, t, cache)
            if not reply:
                continue
            org_id = t.get("organization_id")
            out.append({
                "case_sf_id": str(t["id"]), "tenant_id": tid,
                "case_number": str(t["id"]), "subject": t.get("subject"),
                "body_summary": t.get("description") or t.get("subject") or "",
                "case_type": t.get("type"), "module": None, "region": None,
                "account_id": str(org_id) if org_id else None, "tier": None,
                "resolution_kind": case_memory.classify_resolution_kind(None, reply),
                "resolution_text": reply,
                "generalizable": not case_memory.looks_specific(reply),
                "agent_user_id": str(agent_id) if agent_id else None,
                "resolved_at": t.get("updated_at"), "source": "zendesk",
            })
    return out


def _zendesk_resolution(zc, ticket: dict, cache) -> tuple[str, object | None]:
    tid, req = ticket["id"], ticket.get("requester_id")
    try:
        comments = (zc.request("GET", f"/tickets/{tid}/comments.json")
                    .get("comments") or [])
    except Exception as e:  # noqa: BLE001
        log.warning("zendesk ticket %s comments: %s", tid, e)
        return "", None
    for c in reversed(comments):
        if not c.get("public", True):
            continue
        aid = c.get("author_id")
        if aid == req or (cache.user(aid).get("role") or "") == "end-user":
            continue
        body = (c.get("body") or c.get("plain_body") or "").strip()
        if body:
            return body, aid
    return "", None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingestion.case_memory_sync")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--since", default=None, help="ISO date; default = 90 days ago")
    ap.add_argument("--until", default=None, help="ISO date; ClosedDate < (bounded pull)")
    ap.add_argument("--tenant", default=None,
                    help="tenant_id to tag rows with + resolve the SF org for "
                         "(--from-salesforce)")
    ap.add_argument("--all", action="store_true",
                    help="--from-salesforce: every workspace with a Salesforce "
                         "connection, each against its own org")
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--from-salesforce", action="store_true")
    ap.add_argument("--from-zendesk", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reindex-stale", type=int, metavar="DAYS", default=0,
                    help="mark case_memory rows older than DAYS as status='stale' and exit")
    args = ap.parse_args(argv)

    if args.reindex_stale:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.reindex_stale)).isoformat()
        res = (get_supabase().table("case_memory").update({"status": "stale"})
               .lt("resolved_at", cutoff).eq("status", "active").execute())
        log.info("marked %d row(s) stale (resolved before %s)", len(res.data or []), cutoff[:10])
        return 0

    since = args.since or (datetime.now(timezone.utc) - timedelta(days=90)).date().isoformat()
    since_iso = since if "T" in since else f"{since}T00:00:00Z"
    until_iso = None
    if args.until:
        until_iso = args.until if "T" in args.until else f"{args.until}T00:00:00Z"

    if args.from_zendesk:
        rows = _from_zendesk(since_iso, args.limit)
    elif args.from_salesforce and args.all:
        from interpreter import salesforce
        rows = []
        for tid in salesforce.syncable_tenants(get_supabase()):
            log.info("--from-salesforce --all: workspace %s", tid)
            rows += [r for r in _from_salesforce(
                since_iso, args.limit, until_iso=until_iso, tenant_id=tid) if r]
    elif args.from_salesforce:
        rows = [r for r in _from_salesforce(
            since_iso, args.limit, until_iso=until_iso, tenant_id=args.tenant) if r]
    else:
        rows = [r for r in (_row_from_run(x) for x in _iter_runs(get_supabase(), since_iso, args.limit)) if r]

    log.info("%d candidate resolution(s) %s", len(rows),
             f"in [{since_iso}, {until_iso})" if until_iso else f"since {since_iso}")
    n = _sync_rows(rows, dry=args.dry_run)
    log.info("%s %d case_memory row(s)", "would sync" if args.dry_run else "synced", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
