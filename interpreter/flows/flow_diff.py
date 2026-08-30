"""
Phase 19c -- a structural diff between two flow graphs, for the "AI edit"
preview. Nodes are compared by ``node_id`` (an edit keeps ids stable for
nodes it keeps); edges by ``(source, target, if)``.
"""

from __future__ import annotations

from typing import Any


def _canon(o: Any) -> Any:
    if isinstance(o, dict):
        return tuple(sorted((k, _canon(v)) for k, v in o.items() if not str(k).startswith("_")))
    if isinstance(o, (list, tuple)):
        return tuple(_canon(v) for v in o)
    return o


def _node_sig(n: dict) -> tuple:
    return (n.get("type"), n.get("label") or "", _canon(n.get("config") or {}))


def _edge_sig(e: dict) -> tuple:
    return (
        e.get("source_node_id"),
        e.get("target_node_id"),
        (e.get("condition") or {}).get("if") or "",
    )


def _label(n: dict) -> str:
    return n.get("label") or n.get("type") or str(n.get("node_id", ""))[:8]


def diff_graphs(before: dict, after: dict) -> dict:
    """``{added_nodes, removed_nodes, changed_nodes, added_edges, removed_edges}``.

    ``added/removed/changed`` are node *labels* (for display); the edge
    figures are counts.
    """
    b = {n["node_id"]: n for n in before.get("nodes", [])}
    a = {n["node_id"]: n for n in after.get("nodes", [])}

    added = [_label(a[i]) for i in a if i not in b]
    removed = [_label(b[i]) for i in b if i not in a]
    changed = [
        _label(a[i]) for i in a
        if i in b and _node_sig(a[i]) != _node_sig(b[i])
    ]

    be = {_edge_sig(e) for e in before.get("edges", [])}
    ae = {_edge_sig(e) for e in after.get("edges", [])}

    return {
        "added_nodes": sorted(added),
        "removed_nodes": sorted(removed),
        "changed_nodes": sorted(changed),
        "added_edges": len(ae - be),
        "removed_edges": len(be - ae),
    }
