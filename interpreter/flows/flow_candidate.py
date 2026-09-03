"""
Phase 19 — assemble a *candidate* flow graph from an untrusted source
(a Mermaid diagram, an LLM's JSON) into the DB node/edge shape the editor
loads, without persisting anything.

Both entry points (`mermaid_import`, `assist`) hand `assemble_candidate` a
list of loose node dicts keyed by an author-chosen string (`key`) and a
list of loose edges referencing those keys. This:

  * assigns a real uuid to every new key -- an existing uuid key passes
    through unchanged, so an AI *edit* keeps node identity;
  * resolves each `type` against the handler registry -- an unknown type is
    kept but coerced to ``"draft"`` and flagged (never dropped, never a
    hard failure), matching the Phase 19 decision;
  * merges per-type default config *under* any author-supplied config;
  * remaps the edges onto the uuids and de-dupes them;
  * runs the same structural validator the API uses (`check_flow`) plus the
    builder's single-entry-point rule, splitting the result into `errors`
    (blocking) and `warnings` (advisory).

Returns ``{"nodes": [...], "edges": [...], "warnings": [...], "errors": [...]}``.
The caller returns this straight to the web editor, which loads the graph
as unsaved canvas state -- Save / Publish still go through the normal
validated path.
"""

from __future__ import annotations

import copy
import uuid as _uuid

from interpreter.flows.validate_flow import Flow, check_flow
from interpreter.registry import known_types

FALLBACK_TYPE = "draft"


def _is_uuid(s: object) -> bool:
    try:
        _uuid.UUID(str(s))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def assemble_candidate(
    raw_nodes: list[dict],
    raw_edges: list[dict],
    *,
    defaults: dict[str, dict] | None = None,
) -> dict:
    """Loose nodes/edges -> a candidate flow graph. See module docstring."""
    defaults = defaults or {}
    warnings: list[str] = []
    errors: list[str] = []

    known = known_types()
    id_for: dict[str, str] = {}
    nodes: list[dict] = []

    for i, rn in enumerate(raw_nodes or []):
        if not isinstance(rn, dict):
            warnings.append(f"node #{i}: not an object ({rn!r}) -- skipped")
            continue
        key = str(rn.get("key") or rn.get("node_id") or rn.get("id") or f"n{i}")
        if _is_uuid(key):
            node_id = key
        else:
            node_id = id_for.get(key) or str(_uuid.uuid4())
        id_for[key] = node_id

        rtype = str(rn.get("type") or "").strip()
        label = str(rn.get("label") or "").strip() or rtype or key
        if rtype in known:
            ntype = rtype
        else:
            ntype = FALLBACK_TYPE
            warnings.append(
                f"node {label!r}: type {rtype or '(none)'!r} is not a known node "
                f"type -- set to {FALLBACK_TYPE!r}; pick the right type in the Inspector"
            )

        cfg = copy.deepcopy(defaults.get(ntype, {}))
        supplied = rn.get("config")
        if isinstance(supplied, dict):
            cfg.update(supplied)

        nodes.append({
            "node_id": node_id, "type": ntype, "label": label,
            "position_x": None, "position_y": None, "config": cfg,
        })

    seen: set[tuple] = set()
    edges: list[dict] = []
    for i, re_ in enumerate(raw_edges or []):
        if not isinstance(re_, dict):
            warnings.append(f"edge #{i}: not an object ({re_!r}) -- skipped")
            continue
        s_key = str(re_.get("source") or re_.get("source_node_id") or "")
        t_key = str(re_.get("target") or re_.get("target_node_id") or "")
        s = id_for.get(s_key) or (s_key if _is_uuid(s_key) else None)
        t = id_for.get(t_key) or (t_key if _is_uuid(t_key) else None)
        if not s or not t:
            warnings.append(
                f"edge {s_key or '?'} -> {t_key or '?'} refers to a node that "
                f"isn't in the graph -- dropped"
            )
            continue

        cond = re_.get("condition")
        if not isinstance(cond, dict):
            iff = re_.get("if")
            cond = {"if": str(iff).strip()} if isinstance(iff, str) and iff.strip() else {}
        elif cond.get("if") in (None, ""):
            cond = {k: v for k, v in cond.items() if k != "if"}

        dedupe = (s, t, cond.get("if", ""))
        if dedupe in seen:
            continue
        seen.add(dedupe)
        edges.append({
            "edge_id": str(_uuid.uuid4()),
            "source_node_id": s, "target_node_id": t, "condition": cond,
        })

    # structural validation -- the same check the PUT /flows/{id} path runs
    flow_dict = {
        "flow_id": str(_uuid.uuid4()), "tenant_id": str(_uuid.uuid4()),
        "team": "support", "name": "candidate", "version": 1, "status": "draft",
        "nodes": [{"node_id": n["node_id"], "type": n["type"],
                   "label": n["label"], "config": n["config"]} for n in nodes],
        "edges": edges,
    }
    try:
        parsed = Flow.model_validate(flow_dict)
        errors.extend(check_flow(parsed, require_expected_types=False))
    except Exception as e:  # noqa: BLE001
        errors.append(f"shape: {e}")

    # the builder needs exactly one entry node (no incoming edge). check_flow
    # doesn't enforce this (a CSM flow can legitimately have several roots at
    # design time) -- surface it as advisory so a fresh author isn't stuck.
    if nodes:
        in_ids = {e["target_node_id"] for e in edges}
        roots = [n for n in nodes if n["node_id"] not in in_ids]
        if not roots:
            errors.append(
                "no start node -- every node has an incoming edge (a cycle, or a "
                "missing first step); a runnable flow needs exactly one start"
            )
        elif len(roots) > 1:
            warnings.append(
                "several possible start nodes ("
                + ", ".join(sorted(r["label"] for r in roots))
                + ") -- before publishing, wire them so exactly one node has no "
                "incoming edge"
            )

    return {"nodes": nodes, "edges": edges, "warnings": warnings, "errors": errors}
