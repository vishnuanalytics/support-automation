"""
Multi-provider connectors, step 3 — a small, generic, channel-agnostic
mapping (migration `085`): `(tenant_id, channel, thread_key) -> case_ref`,
so a chat channel (Freshchat, ...) reuses the same case across a
long-lived conversation instead of creating a new one per inbound message.

Deliberately its own tiny module, not folded into `case_events.py` (an
append-only audit log, wrong shape for "look up the current mapping") or
any one channel's own module (this is shared by every present/future chat
channel, not Freshchat-specific). Best-effort like the rest of the
case-control-plane layer (`case_events.record`, `_cp_write`) — a failed
read/write here degrades to "treat this as a new conversation," never
raises and never blocks a run.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("interpreter.channel_threads")


def _sb():
    from ingestion.scraper import get_supabase
    return get_supabase()


def get_case_ref(tenant_id: str | None, channel: str, thread_key: str | None, *,
                 sb=None) -> dict[str, Any] | None:
    """`{"case_ref", "case_number"}` for a known thread, or `None` — never
    raises. Offline tests: no live Supabase read with no `sb` passed
    (matches `routing.py`'s `_fetch_rows` guard)."""
    if not (tenant_id and thread_key):
        return None
    if sb is None and "PYTEST_CURRENT_TEST" in os.environ:
        return None
    try:
        rows = ((sb or _sb()).table("channel_threads")
                .select("case_ref,case_number")
                .eq("tenant_id", tenant_id).eq("channel", channel).eq("thread_key", thread_key)
                .execute().data or [])
        return rows[0] if rows else None
    except Exception as e:  # noqa: BLE001
        log.warning("channel_threads.get_case_ref(%s/%s/%s): %s", tenant_id, channel, thread_key, e)
        return None


def link(tenant_id: str | None, channel: str, thread_key: str | None, *,
        case_ref: str | None, case_number: str | None = None, sb=None) -> None:
    """Record (or refresh) the mapping after a run creates/confirms a case
    for this thread. Best-effort — a failed write just means the next
    message on this thread creates a fresh case instead of reusing this
    one; never raises, never blocks the run that's finishing up."""
    if not (tenant_id and thread_key and case_ref):
        return
    if sb is None and "PYTEST_CURRENT_TEST" in os.environ:
        return
    try:
        (sb or _sb()).table("channel_threads").upsert({
            "tenant_id": tenant_id, "channel": channel, "thread_key": thread_key,
            "case_ref": str(case_ref), "case_number": case_number, "updated_at": "now()",
        }, on_conflict="tenant_id,channel,thread_key").execute()
    except Exception as e:  # noqa: BLE001
        log.warning("channel_threads.link(%s/%s/%s): %s", tenant_id, channel, thread_key, e)
