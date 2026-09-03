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
        assert set(r) == {"id", "name", "category", "description", "source"}
        assert r["id"] and r["name"] and r["description"]
        assert r["source"] == "built-in"
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


# ── Phase 28 step 5: user-saved custom templates (flow_templates) ──────
class _Q:
    def __init__(self, rows, sink):
        self._rows = rows
        self._sink = sink
        self._filters: dict = {}
        self._op: tuple | None = None

    def select(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def _matches(self, row):
        return all(row.get(k) == v for k, v in self._filters.items())

    def insert(self, row):
        self._op = ("insert", dict(row))
        return self

    def delete(self):
        self._op = ("delete",)
        return self

    def execute(self):
        if self._op and self._op[0] == "insert":
            full = {"template_id": "new-id", **self._op[1]}
            self._sink.append(full)
            self._rows.append(full)
            return _Result([full])
        if self._op and self._op[0] == "delete":
            deleted = [r for r in self._rows if self._matches(r)]
            self._rows[:] = [r for r in self._rows if not self._matches(r)]
            return _Result(deleted)
        return _Result([r for r in self._rows if self._matches(r)])


class _Result:
    def __init__(self, data):
        self.data = data


class _SB:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.inserted: list[dict] = []

    def table(self, name):
        assert name == "flow_templates"
        return _Q(self.rows, self.inserted)


def test_save_as_template_inserts_the_right_row():
    sb = _SB()
    row = templates.save_as_template(
        sb, "t1", [{"key": "a", "type": "retrieve"}], [],
        name="My Template", description="a saved flow", created_by="u1",
    )
    assert row["name"] == "My Template" and row["tenant_id"] == "t1"
    assert row["category"] == "Custom"   # default
    assert len(sb.inserted) == 1


def test_custom_graph_returns_a_valid_candidate_shape():
    sb = _SB(rows=[{
        "template_id": "tpl1", "tenant_id": "t1", "name": "My Template",
        "nodes": [{"key": "a", "type": "retrieve"}, {"key": "b", "type": "draft"}],
        "edges": [{"source": "a", "target": "b"}],
    }])
    g = templates.custom_graph(sb, "t1", "tpl1")
    assert g is not None
    assert g["name"] == "My Template"
    assert [n["type"] for n in g["nodes"]] == ["retrieve", "draft"]
    assert g["errors"] == []


def test_custom_graph_unknown_id_is_none():
    sb = _SB()
    assert templates.custom_graph(sb, "t1", "does-not-exist") is None


def test_custom_graph_scoped_to_tenant():
    # a template that exists but belongs to a different tenant is invisible
    sb = _SB(rows=[{"template_id": "tpl1", "tenant_id": "other-tenant",
                    "name": "X", "nodes": [], "edges": []}])
    assert templates.custom_graph(sb, "t1", "tpl1") is None


def test_list_custom_shape():
    sb = _SB(rows=[
        {"template_id": "tpl1", "tenant_id": "t1", "name": "A", "category": "Custom",
         "description": "first"},
        {"template_id": "tpl2", "tenant_id": "t1", "name": "B", "category": None,
         "description": None},
    ])
    rows = templates.list_custom(sb, "t1")
    assert [r["id"] for r in rows] == ["tpl1", "tpl2"]
    assert all(r["source"] == "custom" for r in rows)
    assert rows[1]["category"] == "Custom" and rows[1]["description"] == ""


def test_delete_custom_removes_and_reports():
    sb = _SB(rows=[{"template_id": "tpl1", "tenant_id": "t1", "name": "A"}])
    assert templates.delete_custom(sb, "t1", "tpl1") is True
    assert sb.rows == []
    assert templates.delete_custom(sb, "t1", "tpl1") is False   # already gone
