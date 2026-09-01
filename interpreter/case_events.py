"""
Phase 27 — the append-only per-Case audit log (`case_events`, migration 062).

Every pipeline node, sweep action, Slack button, and Omni assignment writes
exactly one row via `record()`. Best-effort: a failed insert logs and returns
None — an audit write must never break the thing it is auditing. Set
`CASE_EVENTS_DISABLED=1` to skip entirely (tests, throwaway loops).

`/api/trace/<case>` folds this table in as the timeline spine.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("interpreter.case_events")

_COLS = (
    "tenant_id", "case_sf_id", "case_number", "actor", "action",
    "from_status", "to_status", "reason", "routed_team",
    "slack_channel", "slack_ts", "run_id", "confidence",
)


def record(
    sb,
    *,
    tenant_id: str | None,
    case_sf_id: str | None,
    actor: str,
    action: str,
    case_number: str | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    reason: str | None = None,
    routed_team: str | None = None,
    slack_channel: str | None = None,
    slack_ts: str | None = None,
    run_id: str | None = None,
    confidence: float | None = None,
) -> int | None:
    """Append one row. Returns the event_id, or None on skip/failure."""
    if os.environ.get("CASE_EVENTS_DISABLED"):
        return None
    # offline unit tests: don't reach for a Supabase client (matches roster.py)
    if "PYTEST_CURRENT_TEST" in os.environ and not os.environ.get("CASE_EVENTS_FORCE"):
        return None
    if not case_sf_id:
        return None
    row: dict[str, Any] = {
        "tenant_id": str(tenant_id) if tenant_id else None,
        "case_sf_id": str(case_sf_id),
        "case_number": case_number,
        "actor": actor,
        "action": action,
        "from_status": from_status,
        "to_status": to_status,
        "reason": (str(reason)[:2000] if reason else None),
        "routed_team": routed_team,
        "slack_channel": slack_channel,
        "slack_ts": slack_ts,
        "run_id": run_id,
    }
    if confidence is not None:
        try:
            row["confidence"] = round(float(confidence), 2)
        except (TypeError, ValueError):
            pass
    try:
        if sb is None:
            from ingestion.scraper import get_supabase

            sb = get_supabase()
        res = sb.table("case_events").insert(row).execute()
        return res.data[0]["event_id"] if res.data else None
    except Exception as e:  # noqa: BLE001 — telemetry must not raise
        log.warning("case_events.record(%s/%s) failed: %s", case_sf_id, action, e)
        return None


def link_run(sb, *, case_sf_id: str, run_id: str, since_iso: str) -> None:
    """Best-effort: stamp `run_id` on the rows a just-finished run's nodes
    wrote (they had no run_id yet). Called from `runs.record_run`."""
    if os.environ.get("CASE_EVENTS_DISABLED") or not (case_sf_id and run_id):
        return
    if "PYTEST_CURRENT_TEST" in os.environ and not os.environ.get("CASE_EVENTS_FORCE"):
        return
    try:
        (sb.table("case_events").update({"run_id": run_id})
         .eq("case_sf_id", str(case_sf_id)).is_("run_id", "null")
         .gte("ts", since_iso).execute())
    except Exception as e:  # noqa: BLE001
        log.warning("case_events.link_run(%s) failed: %s", case_sf_id, e)
