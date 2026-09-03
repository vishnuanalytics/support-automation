"""
P7a — the flow template gallery. Phase 28 step 5 adds the user-saved
counterpart alongside it.

Each `interpreter/flows/templates/*.json` is a ready-made graph: `{id, name,
category, description, nodes, edges}` in the loose candidate shape
(`assemble_candidate`). `graph(id, defaults)` returns the same
`{name, nodes, edges, warnings, errors}` an AI-generate / Mermaid-import does,
so the editor loads it as an unsaved draft and the existing Save/Publish path
takes over — no new persistence.

The built-in gallery (`list_templates`/`graph`) is file-shipped, reviewed at
merge time, available to every tenant. `flow_templates` (migration `078`) is
the opposite: a user's own saved template, private to the tenant that saved
it — `list_custom`/`custom_graph`/`save_as_template`/`delete_custom` below.
Deliberately not cross-tenant (a bigger, security-sensitive decision this
codebase hasn't made anywhere else).
"""

from __future__ import annotations

import functools
import json
import pathlib
from typing import Any

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
    """The built-in gallery — metadata only, no graph."""
    return [{"id": t["id"], "name": t.get("name") or t["id"],
             "category": t.get("category") or "Other",
             "description": t.get("description") or "", "source": "built-in"}
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


# ── Phase 28 step 5: user-saved templates (flow_templates, tenant-private) ──
def list_custom(sb, tenant_id: str) -> list[dict]:
    rows = (sb.table("flow_templates")
            .select("template_id, name, category, description")
            .eq("tenant_id", tenant_id).order("name").execute().data or [])
    return [{"id": r["template_id"], "name": r["name"],
             "category": r.get("category") or "Custom",
             "description": r.get("description") or "", "source": "custom"}
            for r in rows]


def custom_graph(sb, tenant_id: str, template_id: str, *,
                 defaults: dict | None = None) -> dict | None:
    rows = (sb.table("flow_templates").select("name, nodes, edges")
            .eq("tenant_id", tenant_id).eq("template_id", template_id)
            .execute().data or [])
    if not rows:
        return None
    t = rows[0]
    cand = assemble_candidate(t.get("nodes") or [], t.get("edges") or [],
                              defaults=defaults or {})
    cand["name"] = t.get("name") or template_id
    return cand


def save_as_template(sb, tenant_id: str, nodes: list[dict], edges: list[dict], *,
                     name: str, category: str = "Custom",
                     description: str | None = None,
                     created_by: str | None = None) -> dict[str, Any]:
    row = (sb.table("flow_templates").insert({
        "tenant_id": tenant_id, "name": name, "category": category or "Custom",
        "description": description, "nodes": nodes, "edges": edges,
        "created_by": created_by,
    }).execute().data or [{}])[0]
    return row


def delete_custom(sb, tenant_id: str, template_id: str) -> bool:
    res = (sb.table("flow_templates").delete()
           .eq("tenant_id", tenant_id).eq("template_id", template_id).execute())
    return bool(res.data)
