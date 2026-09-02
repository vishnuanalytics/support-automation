"""
P3 (FR-44 groundwork) — one place that decides an approval, whatever the
transport.

Before this, "an `action_requests` row was approved → set status, enqueue the
follow-up job, edit the Slack card" lived in three copies: the Phase-16 signed
HTTP callback (`api/main.slack_interactions`), the Socket-Mode handler
(`slack_socket.dispatch_action` for `kb_*`), and — for review tasks — both
`slack_socket` and the `/api/review-tasks/{id}/resolve` endpoint. All of them
now call in here.

    decide_action_request(sb, ar_id, approve=..., decided_by=...) -> {...}
    resolve_review_task(sb, task_id, status=..., reviewed_by=...) -> {...}
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger("interpreter.approvals")

# action_requests.kind  ->  the worker job that fulfils it on approval
_FULFIL_JOB = {
    "github_issue": "create_github_issue",
    "kb_change": "apply_kb_change",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def decide_action_request(sb, ar_id: str, *, approve: bool, decided_by: str) -> dict:
    """Approve or reject one `action_requests` row. Idempotent: a row that is
    not `pending` returns `{"skipped": <status>}` and enqueues nothing.

    On approval, enqueues the fulfilment job for `ar.kind` (keyed on the row id
    so a double-click is one job). The caller owns the Slack card edit — the
    `slack` dict in the result carries what to say."""
    rows = sb.table("action_requests").select("*").eq("id", ar_id).execute().data or []
    if not rows:
        return {"skipped": "unknown", "ar_id": ar_id}
    ar = rows[0]
    if ar["status"] != "pending":
        return {"skipped": ar["status"], "ar": ar}

    status = "approved" if approve else "rejected"
    sb.table("action_requests").update({
        "status": status, "decided_by": decided_by, "decided_at": _now_iso(),
    }).eq("id", ar_id).execute()

    job_kind = None
    if approve:
        job_kind = _FULFIL_JOB.get(ar.get("kind") or "")
        if job_kind:
            try:
                from interpreter import jobs
                jobs.enqueue(job_kind, {"action_request_id": ar_id},
                             dedupe_key=f"{ar.get('kind')}:{ar_id}", sb=sb)
            except Exception as e:  # noqa: BLE001
                log.warning("decide_action_request enqueue %s: %s", job_kind, e)

    title = (ar.get("payload") or {}).get("title") or ar.get("kind") or "request"
    slack_text = (
        f":hourglass_flowing_sand: *{title}* — approved by {decided_by}, applying…"
        if approve else f":no_entry: *{title}* — rejected by {decided_by}."
    )
    return {"ar": ar, "status": status, "job_kind": job_kind,
            "slack": {"channel": ar.get("slack_channel"), "ts": ar.get("slack_ts"),
                      "text": slack_text}}


def resolve_review_task(sb, task_id: str, *, status: str, reviewed_by: str | None) -> dict:
    """Mark a `review_tasks` row correct / wrong / dismissed. On `correct`,
    draft a KB change and raise it for approval (KIL-d). Returns
    `{"task", "kb_change"}` or `{"skipped": ...}`."""
    from interpreter import kb_writeback, review

    row = review.resolve(sb, task_id, status=status, reviewer_id=reviewed_by)
    if not row:
        return {"skipped": "not open", "task_id": task_id}

    kb_change = None
    if status == "correct":
        try:
            change = kb_writeback.draft_change(row)
            ar = kb_writeback.raise_kb_change(
                sb, tenant_id=row["tenant_id"], task_row=row, change=change)
            kb_change = {"action_request_id": (ar or {}).get("id"), "change": change}
        except Exception as e:  # noqa: BLE001
            log.warning("resolve_review_task kb_writeback for %s: %s", task_id, e)
    return {"task": row, "kb_change": kb_change}
