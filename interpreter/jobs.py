"""
Thin helpers over the `jobs` table (migration 013). Service-role only.

    enqueue("run_flow", {"flow_id": ..., "case": {...}}, dedupe_key=case_id)
    j = claim()                     # -> dict or None  (FOR UPDATE SKIP LOCKED)
    complete(j["job_id"], result)
    fail(j["job_id"], "boom")       # retries until max_attempts, then 'failed'
"""

from __future__ import annotations

from typing import Any

from ingestion.scraper import get_supabase


def enqueue(kind: str, payload: dict[str, Any], *, dedupe_key: str | None = None,
            run_after: str | None = None, sb=None) -> str | None:
    sb = sb or get_supabase()
    row: dict[str, Any] = {"kind": kind, "payload": payload}
    if dedupe_key:
        row["dedupe_key"] = dedupe_key
    if run_after:
        row["run_after"] = run_after
    try:
        res = sb.table("jobs").insert(row).execute()
        return res.data[0]["job_id"] if res.data else None
    except Exception as e:  # noqa: BLE001 -- unique (kind, dedupe_key) => already queued
        if "uq_jobs_dedupe" in str(e):
            return None
        raise


def claim(sb=None, *, job_id: str | None = None) -> dict[str, Any] | None:
    """Claim the oldest claimable job, or -- if `job_id` is given -- that
    specific row only (immune to another consumer of the same queue
    claiming a different job first; see migration 080)."""
    sb = sb or get_supabase()
    if job_id:
        data = sb.rpc("claim_job", {"p_job_id": job_id}).execute().data
    else:
        data = sb.rpc("claim_job").execute().data
    # claim_job() is `RETURNS SETOF jobs`; an empty queue can still come back
    # as a single all-NULL row (PostgREST wraps it). Treat a row with no
    # job_id as "nothing to do".
    if not data:
        return None
    row = data[0] if isinstance(data, list) else data
    if not row or not row.get("job_id"):
        return None
    return row


def complete(job_id: str, result: dict[str, Any] | None = None, sb=None) -> None:
    sb = sb or get_supabase()
    sb.table("jobs").update(
        {"status": "done", "result": result or {}, "error": None, "updated_at": "now()"}
    ).eq("job_id", job_id).execute()


def fail(job_id: str, error: str, *, sb=None) -> None:
    """Back to 'queued' for another attempt, or 'failed' once attempts run out."""
    if not job_id:
        return
    sb = sb or get_supabase()
    row = sb.table("jobs").select("attempts, max_attempts").eq("job_id", job_id).execute().data
    attempts = row[0]["attempts"] if row else 99
    max_attempts = row[0]["max_attempts"] if row else 3
    done = attempts >= max_attempts
    sb.table("jobs").update({
        "status": "failed" if done else "queued",
        "error": error[:2000],
        "locked_at": None,
        "updated_at": "now()",
    }).eq("job_id", job_id).execute()
