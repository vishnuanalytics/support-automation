"""Repair a flow whose edges were duplicated (e.g. by re-applying a seed
migration whose edge INSERTs have no ON CONFLICT guard), then republish it.

2026-09-23: the Globex demo flow (a2a2a2a2-...) got every original edge
twice on 2026-09-05 and that graph was published as v11, so every run of it
failed with FlowBuildError ("N unconditional outgoing edges").

What it does, per flow:
  1. Find draft edges that are exact duplicates (same source, target and
     condition). Keep one of each - preferring an edge_id that appears in the
     last clean published version - and delete the extra copies.
  2. Re-validate the cleaned draft with interpreter.flows.validate_flow's
     check_flow (which now also rejects multiple unconditional edges).
  3. Publish it exactly as POST /api/flows/{id}/publish does: a new immutable
     flow_versions row, published_version + version bumped, an audit entry.
     Earlier versions are left untouched.

Dry run by default; --apply writes.

  python -m scripts.repair_duplicate_flow_edges a2a2a2a2-2222-4222-8222-222222222222
  python -m scripts.repair_duplicate_flow_edges a2a2a2a2-2222-4222-8222-222222222222 --apply
"""

from __future__ import annotations

import argparse
import json

from dotenv import load_dotenv

load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402
from interpreter import audit  # noqa: E402
from interpreter.loader import definition_hash, load_flow  # noqa: E402


def _key(e: dict) -> tuple:
    return (e["source_node_id"], e["target_node_id"], json.dumps(e.get("condition") or {}, sort_keys=True))


def repair(flow_id: str, apply: bool) -> None:
    sb = get_supabase()
    meta = sb.table("flows").select("*").eq("flow_id", flow_id).execute().data[0]
    edges = sb.table("flow_edges").select("*").eq("flow_id", flow_id).execute().data

    # edge_ids of the newest published version that has no duplicates, to keep stable ids
    clean_ids: set[str] = set()
    for v in sb.table("flow_versions").select("version,edges").eq("flow_id", flow_id) \
            .order("version", desc=True).execute().data:
        keys = [_key(e) for e in v["edges"]]
        if len(keys) == len(set(keys)):
            clean_ids = {e.get("edge_id") for e in v["edges"]}
            print(f"last clean published version: v{v['version']}")
            break

    keep: dict[tuple, dict] = {}
    for e in sorted(edges, key=lambda e: (e.get("edge_id") not in clean_ids, str(e.get("edge_id")))):
        keep.setdefault(_key(e), e)
    drop = [e for e in edges if keep[_key(e)] is not e]
    print(f"{meta['name']}: {len(edges)} draft edges, {len(keep)} unique, {len(drop)} duplicate(s) to delete")
    for e in drop:
        print("  delete", e["edge_id"], e["source_node_id"][:8], "->", e["target_node_id"][:8], e.get("condition") or {})
    if not drop:
        print("nothing to repair")
        return
    if not apply:
        print("dry run - re-run with --apply to write")
        return

    for e in drop:
        sb.table("flow_edges").delete().eq("edge_id", e["edge_id"]).eq("flow_id", flow_id).execute()

    draft = load_flow(flow_id=flow_id, sb=sb, status="draft", validate=True)  # raises FlowInvalid if still broken
    prev = sb.table("flow_versions").select("version").eq("flow_id", flow_id) \
        .order("version", desc=True).limit(1).execute().data
    version = (prev[0]["version"] + 1) if prev else 1
    sb.table("flow_versions").insert({
        "flow_id": flow_id, "version": version, "name": draft["name"],
        "nodes": draft["nodes"], "edges": draft["edges"],
        "definition_hash": definition_hash(draft["nodes"], draft["edges"]),
        "created_by": None,
    }).execute()
    sb.table("flows").update({
        "status": "published", "published_version": version, "version": meta["version"] + 1,
    }).eq("flow_id", flow_id).execute()
    audit.record(sb, tenant_id=meta["tenant_id"], action="flow.published",
                 target_type="flow", target_id=flow_id,
                 summary=f"published {meta.get('name') or flow_id} v{version} "
                         f"(repair: removed {len(drop)} duplicated edge(s))",
                 metadata={"version": version, "repair": "duplicate_edges", "removed": len(drop)})
    print(f"published v{version} ({len(draft['edges'])} edges)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("flow_id")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    repair(args.flow_id, args.apply)
