"""
Phase 28 -- the platform activity/audit log (migration `076`).

`record()` is the one write path into `audit_log`, called from api/main.py
right after a mutation succeeds. Best-effort, matching `runs.record_run` /
`case_events`'s callers: a logging failure must never break the real
mutation it's describing.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("interpreter.audit")


def record(sb, *, tenant_id: str, action: str,
          actor_id: str | None = None, actor_email: str | None = None,
          target_type: str | None = None, target_id: str | None = None,
          summary: str | None = None, metadata: dict[str, Any] | None = None) -> None:
    try:
        sb.table("audit_log").insert({
            "tenant_id": str(tenant_id),
            "action": action,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "target_type": target_type,
            "target_id": str(target_id) if target_id is not None else None,
            "summary": summary,
            "metadata": metadata or {},
        }).execute()
    except Exception as e:  # noqa: BLE001 -- never fail the caller over telemetry
        log.warning("audit.record(%s) failed: %s", action, e)
