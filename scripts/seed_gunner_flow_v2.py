"""
Build a v2 of the gunner "salesforce flow" for the UrbanPiper demo.

Starts from the current published flow's graph and applies four deltas:

  1. + an `attachments` node after `sf_case` (Phase 25) — OCR customer
     screenshots into the classify / draft / clarify prompts.
  2. `retrieve` -> top_k 6, left name-less so it fuses ALL of the tenant's
     KB sources (help + API Document + Organization knowledge +
     urbanpiper-docs from scripts/seed_urbanpiper_kb.py) — narrowing to one
     source measured worse on the demo queries.
  3. `clarify` -> `auto_send: true` + `use_checklists: true` — the bot sends
     the checklist's gap questions to the customer itself and runs the loop,
     instead of posting a silent Chatter draft.
  4. `case_lookup` -> `use_graph: true` (the case graph is now populated).

Writes a NEW flow_id (keeps the old one intact as a rollback), then — if the
DB lets it — demotes the old flow to draft and publishes this one as the
tenant's `email` flow. If that last step is blocked, the new flow is left as
a draft to publish from the editor.

    python -m scripts.seed_gunner_flow_v2
    python -m scripts.seed_gunner_flow_v2 --print     # dump the graph, touch nothing
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
NEW_FLOW = "27b0a1c7-0000-4d00-8000-000000000002"
TEAM = "email"
NAME = "UrbanPiper email → Salesforce (v2)"
_NS = uuid.UUID("11111111-2222-3333-4444-555555555555")


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
    by_type: dict[str, str] = {n["type"]: idmap[n["node_id"]] for n in nodes}

    out_nodes = []
    for n in nodes:
        cfg = dict(n.get("config") or {})
        t = n["type"]
        if t == "retrieve":
            cfg = {**cfg, "top_k": 6}          # name-less: all tenant KB sources
        elif t == "clarify":
            cfg = {**cfg, "auto_send": True, "use_checklists": True}
        elif t == "case_lookup":
            cfg = {**cfg, "use_graph": True}
        out_nodes.append({
            "node_id": idmap[n["node_id"]], "flow_id": NEW_FLOW, "type": t,
            "label": n.get("label") or t, "config": cfg,
            "position_x": n.get("position_x"), "position_y": n.get("position_y"),
        })

    # 1. splice an `attachments` node onto the sf_case -> retrieve edge
    att_id = _rid("attachments", "new")
    sf_case_id = by_type.get("sf_case")
    retrieve_id = by_type.get("retrieve")
    out_edges = []
    spliced = False
    for e in edges:
        s, d = idmap[e["source_node_id"]], idmap[e["target_node_id"]]
        if not spliced and s == sf_case_id and d == retrieve_id:
            out_edges.append({"source_node_id": s, "target_node_id": att_id, "condition": e.get("condition")})
            out_edges.append({"source_node_id": att_id, "target_node_id": d, "condition": None})
            spliced = True
        else:
            out_edges.append({"source_node_id": s, "target_node_id": d, "condition": e.get("condition")})
    if spliced:
        ax = next((n["position_x"] for n in out_nodes if n["node_id"] == sf_case_id), 0) or 0
        ay = next((n["position_y"] for n in out_nodes if n["node_id"] == sf_case_id), 0) or 0
        out_nodes.append({
            "node_id": att_id, "flow_id": NEW_FLOW, "type": "attachments",
            "label": "OCR customer screenshots",
            "config": {"source": "auto", "max_images": 5, "ocr": True},
            "position_x": (ax or 0) + 40, "position_y": (ay or 0) + 140,
        })
    else:
        print("! could not find the sf_case -> retrieve edge; attachments node NOT added")

    out_edges = [{**e, "edge_id": _rid(f'{e["source_node_id"]}->{e["target_node_id"]}', "edge")}
                 for e in out_edges]
    for e in out_edges:
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
        print("v2 graph compiles")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"v2 graph does NOT compile: {e}")

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

    try:
        sb.table("flows").update({"status": "draft"}).eq("flow_id", SRC_FLOW).execute()
        sb.table("flows").update({"status": "published", "published_version": 1}) \
            .eq("flow_id", NEW_FLOW).execute()
        print(f"published v1 — {NAME} is now the tenant's `{TEAM}` flow; "
              f"old flow {SRC_FLOW} demoted to draft (rollback: re-publish it)")
    except Exception as e:  # noqa: BLE001
        print(f"! could not swap published flow ({e}).")
        print(f"  the v2 graph is saved as a DRAFT on flow {NEW_FLOW} — "
              f"open it in the editor and Publish (it will replace the old `{TEAM}` flow).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
