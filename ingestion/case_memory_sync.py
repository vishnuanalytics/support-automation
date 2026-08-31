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
    """Fill case_number / case_type / module / tier from Salesforce for rows
    whose `case_sf_id` is a real Id (the `runs` backfill often lacks them)."""
    from interpreter import salesforce
    if not salesforce.available():
        return
    want = {r["case_sf_id"] for r in rows
            if isinstance(r["case_sf_id"], str) and len(r["case_sf_id"]) in (15, 18)
            and not r.get("case_type")}
    if not want:
        return
    try:
        sf = salesforce.client_for(None)
        ids = ", ".join(f"'{salesforce._soql_lit(i)}'" for i in list(want)[:200])
        recs = sf.query(
            "SELECT Id, CaseNumber, Type, Module__c, Region__c, Account.Tier__c "
            f"FROM Case WHERE Id IN ({ids})"
        ).get("records", [])
        by_id = {c["Id"]: c for c in recs}
    except Exception as e:  # noqa: BLE001
        log.warning("SF enrich failed: %s", e)
        return
    for r in rows:
        c = by_id.get(r["case_sf_id"])
        if not c:
            continue
        r["case_number"] = r.get("case_number") or c.get("CaseNumber")
        r["case_type"] = r.get("case_type") or c.get("Type")
        r["module"] = r.get("module") or c.get("Module__c")
        r["region"] = r.get("region") or c.get("Region__c")
        r["tier"] = r.get("tier") or (c.get("Account") or {}).get("Tier__c")


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
            except Exception as e:  # noqa: BLE001
                log.warning("similar lookup for %s: %s", row["case_sf_id"], e)
        case_memory.sync_graph(row, similar)
        done += 1
    return done


def _from_salesforce(since_iso: str, limit: int) -> list[dict]:
    from interpreter import salesforce
    if not salesforce.available():
        log.warning("--from-salesforce: no Salesforce creds")
        return []
    sf = salesforce.client_for(None)
    lit = salesforce._soql_lit(since_iso)
    cases = sf.query(
        "SELECT Id, CaseNumber, Subject, Description, Type, Module__c, Region__c, "
        "ClosedDate, Account.Tier__c FROM Case "
        f"WHERE IsClosed = true AND ClosedDate >= {lit} ORDER BY ClosedDate DESC "
        f"LIMIT {int(limit)}"
    ).get("records", [])
    out = []
    for c in cases:
        em = sf.query(
            "SELECT TextBody FROM EmailMessage WHERE ParentId = "
            f"'{salesforce._soql_lit(c['Id'])}' AND Incoming = false "
            "ORDER BY MessageDate DESC LIMIT 1"
        ).get("records", [])
        reply = (em[0]["TextBody"] if em else "").strip()
        if not reply:
            continue
        out.append({
            "case_sf_id": c["Id"], "tenant_id": "00000000-0000-0000-0000-000000000000",
            "case_number": c.get("CaseNumber"), "subject": c.get("Subject"),
            "body_summary": c.get("Description") or c.get("Subject") or "",
            "case_type": c.get("Type"), "module": c.get("Module__c"),
            "region": c.get("Region__c"),
            "tier": (c.get("Account") or {}).get("Tier__c"),
            "resolution_kind": case_memory.classify_resolution_kind(None, reply),
            "resolution_text": reply,
            "generalizable": not case_memory.looks_specific(reply),
            "agent_user_id": None, "resolved_at": c.get("ClosedDate"), "source": "salesforce",
        })
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingestion.case_memory_sync")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--since", default=None, help="ISO date; default = 90 days ago")
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--from-salesforce", action="store_true")
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

    if args.from_salesforce:
        rows = [r for r in (
            {**c, **{}} for c in _from_salesforce(since_iso, args.limit)) if r]
    else:
        rows = [r for r in (_row_from_run(x) for x in _iter_runs(get_supabase(), since_iso, args.limit)) if r]

    log.info("%d candidate resolution(s) since %s", len(rows), since_iso)
    n = _sync_rows(rows, dry=args.dry_run)
    log.info("%s %d case_memory row(s)", "would sync" if args.dry_run else "synced", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
