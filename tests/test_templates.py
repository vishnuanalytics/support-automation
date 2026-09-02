"""P7a — the flow template gallery."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import templates
from interpreter.builder import build_graph


def test_list_templates_has_the_expected_shape():
    rows = templates.list_templates()
    assert len(rows) >= 4
    for r in rows:
        assert set(r) == {"id", "name", "category", "description"}
        assert r["id"] and r["name"] and r["description"]
    ids = {r["id"] for r in rows}
    assert {"support-autoreply", "webhook-rag-qa"} <= ids


@pytest.mark.parametrize("tid", [r["id"] for r in templates.list_templates()])
def test_every_template_assembles_and_compiles(tid):
    from api.main import NODE_DEFAULTS

    g = templates.graph(tid, defaults=NODE_DEFAULTS)
    assert g is not None
    assert g["errors"] == [], (tid, g["errors"])
    # the assembled graph builds into a real StateGraph
    flow = {
        "flow_id": "t", "tenant_id": "t", "team": "support", "name": g["name"],
        "version": 1, "status": "draft", "nodes": g["nodes"], "edges": g["edges"],
    }
    build_graph(flow)


def test_unknown_template_is_none():
    assert templates.graph("does-not-exist") is None
