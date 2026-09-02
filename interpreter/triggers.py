"""
P5b — turn an external event into a generic `RunContext`.

A flow that doesn't operate on a Salesforce Case starts from a `trigger` node
and reads its input as `context.*` / `input.*` (P5a). These adapters build the
`context` dict the run is invoked with:

    webhook_context({"plan": "free", "email": "a@b.com"})
      -> {"context": {"plan": "free", "email": "a@b.com",
                      "_trigger": "webhook", "_received_at": "2026-…Z"}}

Kept tiny and pure — the transport (an HTTP endpoint, a cron tick) lives in the
API / worker; this just normalises the payload.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

_MAX_BYTES = 64 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(v: Any, depth: int = 0):
    """Keep a webhook body to something sane — strings capped, nesting shallow."""
    if depth > 6:
        return "…"
    if isinstance(v, str):
        return v[:8000]
    if isinstance(v, dict):
        return {str(k)[:200]: _clip(x, depth + 1) for k, x in list(v.items())[:200]}
    if isinstance(v, (list, tuple)):
        return [_clip(x, depth + 1) for x in list(v)[:200]]
    return v


def webhook_context(body: dict[str, Any] | None, *, source: str = "webhook") -> dict[str, Any]:
    """`{"context": <normalised body + trigger metadata>}` — the dict a flow is
    invoked with (`builder.initial_state(flow, context=...)`)."""
    ctx = _clip(body or {})
    if not isinstance(ctx, dict):
        ctx = {"value": ctx}
    ctx.setdefault("_trigger", source)
    ctx["_received_at"] = _now()
    return {"context": ctx}


def schedule_context(*, params: dict[str, Any] | None = None, cron: str | None = None) -> dict[str, Any]:
    ctx = _clip(params or {})
    ctx.update({"_trigger": "schedule", "_cron": cron, "_received_at": _now()})
    return {"context": ctx}
