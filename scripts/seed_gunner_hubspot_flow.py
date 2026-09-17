"""
SUPERSEDED same day by the `tenants.channel_connector_map` design (migration
110) — the flow this script produces was demoted back to draft (id
27b0a1c7-0000-4d00-8000-000000000003; the gunner tenant's real entry flow
is `117989a7-289a-44ac-bf0a-6ea4a7b51bc2` again, unchanged). Kept here as a
historical build artifact (same convention as `seed_gunner_flow_v2.py`,
also never re-run) — do NOT run this again as-is; a tenant needing
per-channel connector routing today should use `channel_connector_map`
(Connections tab -> "Route by channel", or `PUT /api/tenants/channel-
connector-map`) on the ONE existing flow instead of a duplicated one. See
PROJECT_SCOPE.md's "ONE flow serving multiple connectors" entry for why
this approach was reverted: this project's case-touching nodes are
scattered across a flow, not clustered, so duplicating the graph per
connector means re-duplicating almost the whole flow.

Original docstring, describing what this script actually does (still
accurate as a description of its mechanics, just not the recommended path
anymore):

Build a HubSpot-flavored twin of the gunner "salesforce flow" for the
UrbanPiper demo, per the user's request ("another editor similar to gunner
workflow with hubspot").

Clones the current published flow's graph structurally UNCHANGED, and adds
an explicit `config.connector = "hubspot"` override to every case-touching
node (the same 8-node set `connectors.py`'s own module docstring documents:
sf_case, sf_writeback, notify, ask_human, handover, identify, clarify,
notify_human) — so this flow always acts on HubSpot regardless of the
tenant's `case_connector` default, without touching that tenant-wide
setting or the original flow at all.

Deliberately does NOT touch `sf_entry` or demote the original flow (unlike
`seed_gunner_flow_v2.py`, which swaps the published flow outright) — the
gunner tenant's live Salesforce entry point must keep working unchanged.
This flow gets its own `team` ("hubspot", not "email") specifically so it
doesn't collide with `load_flow(tenant_id, team, status="published")`
lookups, which require exactly one match per (tenant, team, status).

    python -m scripts.seed_gunner_hubspot_flow
    python -m scripts.seed_gunner_hubspot_flow --print     # dump the graph, touch nothing
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import uuid

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

TENANT = "ee4102db-ab47-4deb-b28b-9efc5a02575b"
SRC_FLOW = "117989a7-289a-44ac-bf0a-6ea4a7b51bc2"
NEW_FLOW = "27b0a1c7-0000-4d00-8000-000000000003"
TEAM = "hubspot"
NAME = "UrbanPiper email -> HubSpot"
_NS = uuid.UUID("11111111-2222-3333-4444-555555555556")

# the exact 8-node case-touching contract connectors.py's own module
# docstring documents — every other node type (retrieve/classify/draft/
# team_route/confidence_gate/case_lookup/...) doesn't call connectors.invoke
# and needs no override.
CASE_TOUCHING_TYPES = {
    "sf_case", "sf_writeback", "notify", "ask_human",
    "handover", "identify", "clarify", "notify_human",
}


def _rid(old: str, salt: str = "") -> str:
    return str(uuid.uuid5(_NS, f"{NEW_FLOW}:{salt}:{old}"))


def build() -> dict:
    from ingestion.scraper import get_supabase

    sb = get_supabase()
    nodes = (sb.table("flow_nodes")
             .select("node_id,type,label,config,position_x,position_y")
             .eq("flow_id", SRC_FLOW).execute().data or [])
    edges = (sb.table("flow_edges")
             .select("source_node_id,target_node_id,condition")
             .eq("flow_id", SRC_FLOW).execute().data or [])
    if not nodes:
        sys.exit(f"source flow {SRC_FLOW} has no nodes")

    idmap = {n["node_id"]: _rid(n["node_id"]) for n in nodes}

    out_nodes = []
    for n in nodes:
        cfg = dict(n.get("config") or {})
        if n["type"] in CASE_TOUCHING_TYPES:
            cfg["connector"] = "hubspot"
        out_nodes.append({
            "node_id": idmap[n["node_id"]], "flow_id": NEW_FLOW, "type": n["type"],
            "label": n.get("label") or n["type"], "config": cfg,
            "position_x": n.get("position_x"), "position_y": n.get("position_y"),
        })

    out_edges = [{"source_node_id": idmap[e["source_node_id"]],
                 "target_node_id": idmap[e["target_node_id"]],
                 "condition": e.get("condition")} for e in edges]
    for e in out_edges:
        e["edge_id"] = _rid(f'{e["source_node_id"]}->{e["target_node_id"]}', "edge")
        e["flow_id"] = NEW_FLOW
    return {"nodes": out_nodes, "edges": out_edges}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", dest="dump")
    args = ap.parse_args()

    graph = build()
    if args.dump:
        print(json.dumps(graph, indent=2, default=str))
        return 0

    from ingestion.scraper import get_supabase
    from interpreter.builder import build_graph
    from interpreter.loader import definition_hash

    try:
        build_graph({"tenant_id": TENANT, "team": TEAM,
                     "nodes": graph["nodes"], "edges": graph["edges"]})
        print("hubspot-flavored graph compiles")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"graph does NOT compile: {e}")

    sb = get_supabase()
    sb.table("flows").upsert({
        "flow_id": NEW_FLOW, "tenant_id": TENANT, "team": TEAM, "name": NAME,
        "status": "draft",
    }, on_conflict="flow_id").execute()
    sb.rpc("replace_flow_graph", {
        "p_flow_id": NEW_FLOW, "p_nodes": graph["nodes"], "p_edges": graph["edges"],
    }).execute()
    print(f"draft written: {len(graph['nodes'])} nodes / {len(graph['edges'])} edges  (flow {NEW_FLOW})")

    live_n = (sb.table("flow_nodes").select("node_id,type,label,config")
              .eq("flow_id", NEW_FLOW).execute().data)
    live_e = (sb.table("flow_edges").select("edge_id,source_node_id,target_node_id,condition")
              .eq("flow_id", NEW_FLOW).execute().data)
    h = definition_hash(live_n, live_e)
    sb.table("flow_versions").insert({
        "flow_id": NEW_FLOW, "version": 1, "name": NAME,
        "nodes": live_n, "edges": live_e, "definition_hash": h,
    }).execute()

    # publish as its OWN flow -- never touches the source flow's status or
    # sf_entry, so the gunner tenant's live Salesforce entry point is
    # completely unaffected.
    sb.table("flows").update({"status": "published", "published_version": 1}) \
        .eq("flow_id", NEW_FLOW).execute()
    print(f"published v1 — {NAME} (team={TEAM!r}) exists alongside the salesforce flow; "
          f"neither sf_entry nor the source flow ({SRC_FLOW}) was touched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
