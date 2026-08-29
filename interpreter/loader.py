"""
Load one flow (row + nodes + edges) from Supabase into the same dict shape
as `flow_support_example.json`, and structurally validate it with the Phase 0
validator (`validate_flow.check_flow`) before anyone tries to build a graph.
"""

from __future__ import annotations

import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scraper import get_supabase  # noqa: E402  reuse the service-role client
from validate_flow import Flow, check_flow  # noqa: E402  one validator, not two


class FlowNotFound(LookupError):
    pass


class FlowInvalid(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("flow failed validation:\n  - " + "\n  - ".join(errors))


def load_flow(
    flow_id: str | None = None,
    *,
    tenant_id: str | None = None,
    team: str | None = None,
    status: str = "published",
    sb=None,
) -> dict[str, Any]:
    """
    Fetch a flow by `flow_id`, or by `(tenant_id, team, status)` (defaults to
    the single published flow for that team -- guaranteed unique by the
    `uq_one_published_flow_per_team` index). Returns the flow as a dict:
    {flow_id, tenant_id, team, name, version, status, nodes:[...], edges:[...]}.
    """
    sb = sb or get_supabase()

    q = sb.table("flows").select("*")
    if flow_id:
        q = q.eq("flow_id", flow_id)
    else:
        if not (tenant_id and team):
            raise ValueError("pass either flow_id, or both tenant_id and team")
        q = q.eq("tenant_id", tenant_id).eq("team", team).eq("status", status)
    rows = q.execute().data or []
    if not rows:
        raise FlowNotFound(
            f"no flow for flow_id={flow_id!r} / tenant={tenant_id!r} team={team!r} status={status!r}"
        )
    if len(rows) > 1:
        raise FlowInvalid([f"{len(rows)} flows matched; expected exactly one"])
    row = rows[0]
    fid = row["flow_id"]

    nodes = (
        sb.table("flow_nodes")
        .select("node_id, type, label, position_x, position_y, config")
        .eq("flow_id", fid)
        .execute()
        .data
        or []
    )
    edges = (
        sb.table("flow_edges")
        .select("edge_id, source_node_id, target_node_id, condition")
        .eq("flow_id", fid)
        .execute()
        .data
        or []
    )

    flow_dict: dict[str, Any] = {
        "flow_id": fid,
        "tenant_id": row["tenant_id"],
        "team": row["team"],
        "name": row["name"],
        "version": row["version"],
        "status": row["status"],
        "nodes": [
            {
                "node_id": n["node_id"],
                "type": n["type"],
                "label": n.get("label"),
                "position_x": n.get("position_x"),
                "position_y": n.get("position_y"),
                "config": n.get("config") or {},
            }
            for n in nodes
        ],
        "edges": [
            {
                "edge_id": e["edge_id"],
                "source_node_id": e["source_node_id"],
                "target_node_id": e["target_node_id"],
                "condition": e.get("condition") or {},
            }
            for e in edges
        ],
    }

    parsed = Flow.model_validate(flow_dict)
    # The interpreter runs arbitrary flows (a CSM / offboarding flow needn't
    # have a confidence_gate), so only the hard checks apply here: referential
    # integrity, no orphans, no cycles. EXPECTED_TYPES is a "does this look
    # like the canonical support flow" convention kept for validate_flow.py's
    # CLI on flow_support_example.json.
    errors = check_flow(parsed, require_expected_types=False)
    if errors:
        raise FlowInvalid(errors)

    return flow_dict


def list_flows(tenant_id: str | None = None, *, status: str | None = None, sb=None) -> list[dict]:
    """Lightweight listing (service-role): flow_id/tenant/team/name/status/version."""
    sb = sb or get_supabase()
    q = sb.table("flows").select("flow_id, tenant_id, team, name, status, version")
    if tenant_id:
        q = q.eq("tenant_id", tenant_id)
    if status:
        q = q.eq("status", status)
    return sorted(
        q.execute().data or [],
        key=lambda r: (r["tenant_id"], r["team"], r["name"]),
    )
