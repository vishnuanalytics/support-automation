"""
Phase 23 — a heartbeat every long-running process writes, so a dead pipeline
is visible (it ran silently for ~40 min once). `scripts/health_check.py`
reads it and alerts.

    from interpreter.health import beat
    beat("worker", {"queue_empty": True})     # cheap, throttled, never raises
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("interpreter.health")

_MIN_INTERVAL_S = float(os.environ.get("HEALTH_BEAT_MIN_S", "20"))
_last: dict[str, float] = {}


def beat(component: str, detail: dict[str, Any] | None = None, *, sb=None, force: bool = False) -> None:
    """Upsert `system_health[component].last_healthy_at = now`. Throttled to
    once per `HEALTH_BEAT_MIN_S`; swallows every error."""
    now = time.monotonic()
    if not force and now - _last.get(component, 0.0) < _MIN_INTERVAL_S:
        return
    _last[component] = now
    try:
        sb = sb or _sb()
        sb.table("system_health").upsert({
            "component": component,
            "last_healthy_at": datetime.now(timezone.utc).isoformat(),
            "detail": detail or {},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="component").execute()
    except Exception as e:  # noqa: BLE001
        log.debug("health.beat(%s) failed: %s", component, e)


def _sb():
    from ingestion.scraper import get_supabase

    return get_supabase()
