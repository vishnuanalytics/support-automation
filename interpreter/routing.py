"""
Phase 20o — resolve the `notify` node's internal-rep target from a central
per-tenant table (`notify_targets`) instead of per-flow node config.

A row maps a `Case.Type` (or `Module__c`) to one of:
  * resolver='static'       -> a fixed Salesforce User/Group id
  * resolver='sf_team_role' -> the current member of a Phase 20i team queue
                               (Team_<team>), looked up LIVE from Salesforce
  * resolver='sf_queue'     -> a Salesforce Queue, by DeveloperName / Name

`interpreter.registry.h_notify` still honours a matching entry in the node's
own `target_by_type` / `target_by_module` (a per-flow override); it falls back
to this resolver when the node config has no match. Everything degrades to
`None` (no DB, no SF creds, a failed query) — `h_notify` then uses its
`fallback_target` / a bare label.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from interpreter import salesforce

log = logging.getLogger("interpreter.routing")

# TTL cache — routing config + the SF lookups it does barely change, and this
# runs on every escalation. Keeps us well under the Dev-Edition API cap.
_TTL_S = float(os.environ.get("NOTIFY_ROUTE_TTL_S", "300"))
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any:
    hit = _cache.get(key)
    return hit[1] if hit and hit[0] > time.monotonic() else None


def _cache_put(key: str, value: Any) -> None:
    _cache[key] = (time.monotonic() + _TTL_S, value)


def _default_sb():
    from ingestion.scraper import get_supabase

    return get_supabase()


def _fetch_rows(tenant_id: str | None, sb) -> list[dict[str, Any]]:
    try:
        sb = sb or _default_sb()
        q = (sb.table("notify_targets")
             .select("match_kind,match_value,resolver,sf_target_id,sf_target_type,"
                     "sf_team,sf_role,sf_queue,label,active")
             .eq("active", True))
        if tenant_id:
            q = q.eq("tenant_id", str(tenant_id))
        return q.execute().data or []
    except Exception as e:  # noqa: BLE001 — routing config is best-effort
        log.warning("notify_targets fetch failed: %s", e)
        return []


def _sf_team_member(team: str | None, tenant_id: str | None) -> tuple[str | None, str | None]:
    """The active User in the `Team_<team>` queue (the roster manager).

    Two hops — SOQL forbids a nested semi-join sub-select, so resolve the
    queue's Group id first, then the User that is a member of it.
    """
    if not (team and salesforce.available()):
        return None, None
    try:
        sf = salesforce.client_for(tenant_id)
        dev = salesforce._soql_lit(f"Team_{team}")
        grp = sf.query(
            f"SELECT Id FROM Group WHERE Type = 'Queue' AND DeveloperName = '{dev}' LIMIT 1"
        ).get("records", [])
        if not grp:
            return None, None
        gid = salesforce._soql_lit(grp[0]["Id"])
        rows = sf.query(
            "SELECT Id, Name FROM User WHERE IsActive = true AND Id IN "
            f"(SELECT UserOrGroupId FROM GroupMember WHERE GroupId = '{gid}') LIMIT 1"
        ).get("records", [])
        if rows:
            return rows[0]["Id"], rows[0].get("Name")
    except Exception as e:  # noqa: BLE001
        log.warning("notify sf_team_role resolve (%s): %s", team, e)
    return None, None


def _sf_queue_id(name: str | None, tenant_id: str | None) -> str | None:
    if not (name and salesforce.available()):
        return None
    try:
        sf = salesforce.client_for(tenant_id)
        q = salesforce._soql_lit(name)
        rows = sf.query(
            "SELECT Id FROM Group WHERE Type = 'Queue' AND "
            f"(DeveloperName = '{q}' OR Name = '{q}') LIMIT 1"
        ).get("records", [])
        if rows:
            return rows[0]["Id"]
    except Exception as e:  # noqa: BLE001
        log.warning("notify sf_queue resolve (%s): %s", name, e)
    return None


def resolve_notify_target(
    tenant_id: str | None,
    case_type: str | None,
    module: str | None = None,
    *,
    sb=None,
) -> dict[str, Any] | None:
    """`Case.Type` (then `Module__c`) -> the internal party to ping.

    Returns `{"id", "type", "label", "resolver"}` (id/type may be `None` when
    the row is note-only or the live lookup found nobody), or `None` when no
    row matches / the table is unavailable. Result is cached `NOTIFY_ROUTE_TTL_S`
    (default 300 s) so an escalation storm doesn't hammer the SF API.
    """
    ck = f"resolve:{tenant_id}:{case_type}:{module}"
    cached = _cache_get(ck)
    if cached is not None:
        return None if cached == "__none__" else cached

    rows = _fetch_rows(tenant_id, sb)
    if not rows:
        return None                       # table down — don't cache a transient miss
    by_type = {r["match_value"]: r for r in rows if r.get("match_kind") == "case_type"}
    by_mod = {r["match_value"]: r for r in rows if r.get("match_kind") == "module"}
    row = (case_type and by_type.get(case_type)) or (module and by_mod.get(module))
    if not row:
        _cache_put(ck, "__none__")
        return None

    resolver = row.get("resolver") or "static"
    out: dict[str, Any] = {
        "id": None,
        "type": None,
        "label": row.get("label") or case_type or module or "support",
        "resolver": resolver,
    }

    if resolver == "static":
        out["id"] = row.get("sf_target_id") or None
        out["type"] = row.get("sf_target_type") or ("user" if out["id"] else None)
    elif resolver == "sf_queue":
        qid = _sf_queue_id(row.get("sf_queue"), tenant_id)
        out["id"], out["type"] = qid, ("queue" if qid else None)
    elif resolver == "sf_team_role":
        uid, name = _sf_team_member(row.get("sf_team"), tenant_id)
        out["id"], out["type"] = uid, ("user" if uid else None)
        if name and not row.get("label"):
            role = row.get("sf_role") or "Manager"
            out["label"] = f"{name} ({row['sf_team']} {role})"

    _cache_put(ck, out)
    return out
