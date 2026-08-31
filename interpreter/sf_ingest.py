"""
Salesforce → automation ingest: resolve the entry flow and queue a run.

One code path shared by both push mechanisms:
  * `POST /api/hooks/salesforce/case`  (Phase 20i — Apex/Flow HTTP callout)
  * `ingestion.sf_cdc_watch`           (Phase 20l — Pub/Sub API CDC stream)

Both drop a `run_flow` job that `api.worker` drains. The job's `dedupe_key`
(one live job per key) and the run's `idempotency_key` (one recorded run
per (flow_id, key)) are passed in by the caller so each *event* — Case
created, inbound email, queue change — is processed once, while a
redelivered copy of the same event is a no-op.
"""

from __future__ import annotations

from interpreter import jobs


class EntryFlowError(RuntimeError):
    """No single published flow is marked `sf_entry`."""


def resolve_entry_flow_id(sb) -> str:
    """The flow the Salesforce Case pipeline runs — the one the editor's
    "Salesforce entry" toggle points at (`flows.sf_entry`, migration 042)."""
    rows = (
        sb.table("flows").select("flow_id")
        .eq("sf_entry", True).eq("status", "published").execute().data
        or []
    )
    if len(rows) != 1:
        raise EntryFlowError(
            f"expected exactly one published flow marked 'Salesforce entry', found {len(rows)} "
            "— set one with the toggle in the flow editor (PUT /api/flows/{id}/sf-entry)"
        )
    return rows[0]["flow_id"]


def enqueue_case_run(
    sb,
    case_id: str,
    *,
    dedupe_key: str,
    idempotency_key: str,
    trigger: str = "",
    flow_id: str | None = None,
) -> str | None:
    """Queue a `run_flow` job for a Salesforce Case. Returns the job id, or
    `None` when an identical job is already queued (dedupe). The worker
    hydrates the bare Case id via `salesforce.get_case`."""
    fid = flow_id or resolve_entry_flow_id(sb)
    return jobs.enqueue(
        "run_flow",
        {
            "flow_id": fid,
            "case": {"sf_id": case_id, "id": case_id, "channel": "salesforce"},
            "idempotency_key": idempotency_key,
            "trigger": trigger,
        },
        dedupe_key=dedupe_key,
        sb=sb,
    )
