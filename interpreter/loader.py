"""
Load one flow (row + nodes + edges) from Supabase into the same dict shape
as `flow_support_example.json`, and structurally validate it with the Phase 0
validator (`validate_flow.check_flow`) before anyone tries to build a graph.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from ingestion.scraper import get_supabase  # noqa: E402  reuse the service-role client
from interpreter.flows.validate_flow import Flow, check_flow  # noqa: E402  one validator, not two


def definition_hash(nodes: list[dict], edges: list[dict]) -> str:
    """Stable sha256 of a flow's graph — order-independent, ignores positions."""
    def norm_node(n: dict) -> dict:
        return {"node_id": n["node_id"], "type": n["type"], "label": n.get("label"),
                "config": n.get("config") or {}}

    def norm_edge(e: dict) -> dict:
        return {"edge_id": e["edge_id"], "source_node_id": e["source_node_id"],
                "target_node_id": e["target_node_id"], "condition": e.get("condition") or {}}

    payload = {
        "nodes": sorted((norm_node(n) for n in nodes), key=lambda x: x["node_id"]),
        "edges": sorted((norm_edge(e) for e in edges), key=lambda x: x["edge_id"]),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class FlowNotFound(LookupError):
    pass


class FlowInvalid(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("flow failed validation:\n  - " + "\n  - ".join(errors))


def _draft_graph(sb, fid: str) -> tuple[list[dict], list[dict]]:
    """The live, editable graph (flow_nodes / flow_edges)."""
    nodes = (
        sb.table("flow_nodes")
        .select("node_id, type, label, position_x, position_y, config")
        .eq("flow_id", fid).execute().data or []
    )
    edges = (
        sb.table("flow_edges")
        .select("edge_id, source_node_id, target_node_id, condition")
        .eq("flow_id", fid).execute().data or []
    )
    return nodes, edges


def _version_graph(sb, fid: str, version: int) -> tuple[list[dict], list[dict], int]:
    """An immutable published snapshot (flow_versions)."""
    rows = (
        sb.table("flow_versions").select("version, nodes, edges")
        .eq("flow_id", fid).eq("version", version).execute().data or []
    )
    if not rows:
        raise FlowNotFound(f"flow {fid} has no version {version}")
    r = rows[0]
    return r["nodes"] or [], r["edges"] or [], r["version"]


def load_flow(
    flow_id: str | None = None,
    *,
    tenant_id: str | None = None,
    team: str | None = None,
    status: str = "published",
    version: int | None = None,
    sb=None,
    validate: bool = True,
) -> dict[str, Any]:
    """
    Fetch a flow by `flow_id`, or by `(tenant_id, team, status)`. Returns
    {flow_id, tenant_id, team, name, version, status, flow_version, nodes, edges}.

    Which graph:
      * status="published" (default) -> the immutable snapshot at
        `flows.published_version` (or an explicit `version=`). This is what a
        run executes.
      * status="draft" (or a published flow with no published_version) -> the
        live `flow_nodes`/`flow_edges` working draft (what the editor edits).

    `sb` may be a user-scoped client (RLS applies). `validate=False` skips the
    structural check (the editor loads work-in-progress flows).
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

    want_version = version if version is not None else row.get("published_version")
    use_snapshot = status != "draft" and want_version is not None
    if use_snapshot:
        nodes, edges, flow_version = _version_graph(sb, fid, want_version)
    else:
        nodes, edges = _draft_graph(sb, fid)
        flow_version = None

    flow_dict: dict[str, Any] = {
        "flow_id": fid,
        "tenant_id": row["tenant_id"],
        "team": row["team"],
        "name": row["name"],
        "version": row["version"],
        "status": row["status"],
        "flow_version": flow_version,
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

    if validate:
        parsed = Flow.model_validate(flow_dict)
        # The interpreter runs arbitrary flows (a CSM / offboarding flow needn't
        # have a confidence_gate), so only the hard checks apply here:
        # referential integrity, no orphans, no cycles. EXPECTED_TYPES is a
        # "does this look like the canonical support flow" convention kept for
        # validate_flow.py's CLI on flow_support_example.json.
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
