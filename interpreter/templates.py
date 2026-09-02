"""
P7a — the flow template gallery.

Each `interpreter/flows/templates/*.json` is a ready-made graph: `{id, name,
category, description, nodes, edges}` in the loose candidate shape
(`assemble_candidate`). `graph(id, defaults)` returns the same
`{name, nodes, edges, warnings, errors}` an AI-generate / Mermaid-import does,
so the editor loads it as an unsaved draft and the existing Save/Publish path
takes over — no new persistence.
"""

from __future__ import annotations

import functools
import json
import pathlib

from interpreter.flows.flow_candidate import assemble_candidate

_DIR = pathlib.Path(__file__).parent / "flows" / "templates"


@functools.lru_cache(maxsize=1)
def _all() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in sorted(_DIR.glob("*.json")):
        try:
            t = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        t.setdefault("id", f.stem)
        out[t["id"]] = t
    return out


def list_templates() -> list[dict]:
    """The gallery — metadata only, no graph."""
    return [{"id": t["id"], "name": t.get("name") or t["id"],
             "category": t.get("category") or "Other",
             "description": t.get("description") or ""}
            for t in _all().values()]


def graph(template_id: str, *, defaults: dict | None = None) -> dict | None:
    """A candidate graph for the editor. None if the id is unknown."""
    t = _all().get(template_id)
    if not t:
        return None
    cand = assemble_candidate(t.get("nodes") or [], t.get("edges") or [],
                              defaults=defaults or {})
    cand["name"] = t.get("name") or template_id
    return cand
