"""
Phase 26 — the live free-model roster.

`scripts/refresh_llm_roster.py` writes `llm_roster` daily from OpenRouter's
catalog (the models that are $0 *today*, ranked). `interpreter/llm.py` reads
the chains from here so a retired `:free` slug drops out on its own.

    from interpreter.roster import chain
    free, premium = chain("vision")      # -> (["google/gemini-…:free", …], ["google/gemini-2.0-flash-001", …])

Cached ~5 min in-process; any failure -> ([], []) so `llm.py` falls back to
its hardcoded env defaults.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

log = logging.getLogger("interpreter.roster")

_TTL_S = 300.0
_cache: dict[str, tuple[float, list[str], list[str]]] = {}
CAPABILITIES = ("text", "vision", "video")


def chain(capability: str, *, sb=None) -> tuple[list[str], list[str]]:
    """(free_models, premium_models) for 'text' | 'vision' | 'video'."""
    # inert under pytest / when disabled — the roster is a prod optimisation and
    # its model ids should never be tried live from the test suite.
    if os.environ.get("ROSTER_DISABLED") or os.environ.get("PYTEST_CURRENT_TEST"):
        return [], []
    now = time.time()
    hit = _cache.get(capability)
    if hit and now - hit[0] < _TTL_S:
        return hit[1], hit[2]
    free: list[str] = []
    premium: list[str] = []
    try:
        if sb is None:
            from ingestion.scraper import get_supabase
            sb = get_supabase()
        rows = (sb.table("llm_roster").select("models, premium")
                .eq("capability", capability).execute().data or [])
        if rows:
            free = [str(x) for x in (rows[0].get("models") or []) if x]
            premium = [str(x) for x in (rows[0].get("premium") or []) if x]
    except Exception as e:  # noqa: BLE001
        log.debug("roster read (%s) failed: %s", capability, e)
    _cache[capability] = (now, free, premium)
    return free, premium


def invalidate() -> None:
    _cache.clear()


def write(sb, capability: str, models: list[str], premium: list[str], *,
          source: str = "openrouter") -> None:
    sb.table("llm_roster").upsert({
        "capability": capability, "models": models, "premium": premium,
        "refreshed_at": "now()", "source": source,
    }, on_conflict="capability").execute()
    invalidate()
