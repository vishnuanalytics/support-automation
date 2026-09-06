"""interpreter/graph_query.py — the composable "ask the case graph" spec,
its deterministic Cypher compiler, and the safety asserts."""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import graph_query as g


# ── validate_spec ─────────────────────────────────────────────────────
def test_validate_defaults_and_clamps():
    s = g.validate_spec({})
    assert s["entity"] == "case" and s["metric"] == "count"
    assert s["group_by"] == [] and s["filters"] == []
    assert s["limit"] == g._LIMIT_DEFAULT

    assert g.validate_spec({"limit": 99999})["limit"] == g._LIMIT_MAX
    assert g.validate_spec({"limit": -3})["limit"] == 1


@pytest.mark.parametrize("bad", [
    {"metric": "bogus"},
    {"group_by": ["not_a_dim"]},
    {"group_by": ["module", "tier", "status"]},          # > 2
    {"filters": [{"field": "evil", "op": "eq", "value": "x"}]},
    {"filters": [{"field": "status", "op": "contains", "value": "x"}]},   # op not allowed
    {"filters": [{"field": "opened_at", "op": "gte", "value": "last week"}]},  # not ISO
    {"filters": [{"field": "tier", "op": "in", "value": "gold"}]},        # in needs a list
    {"metric": "count", "group_by": [], "having": {"op": "gte", "value": 2}},  # having w/o group
    {"having": {"op": "gte", "value": 2}, "group_by": ["tier"], "metric": "count",
     "order_by": {"field": "nonsense", "dir": "desc"}},
])
def test_validate_rejects(bad):
    with pytest.raises(g.GraphQueryError):
        g.validate_spec(bad)


def test_validate_list_drops_group_by():
    s = g.validate_spec({"metric": "list", "group_by": ["tier"]})
    assert s["group_by"] == []


def test_validate_coerces_bool_and_in_list():
    s = g.validate_spec({"filters": [
        {"field": "is_closed", "op": "eq", "value": "true"},
        {"field": "tier", "op": "in", "value": ["gold", 2]},
    ]})
    assert s["filters"][0]["value"] is True
    assert s["filters"][1]["value"] == ["gold", "2"]


# ── compile_spec — the safety-critical part ───────────────────────────
_ALL_SHAPES = [
    {"metric": "count"},
    {"metric": "count", "filters": [{"field": "module", "op": "eq", "value": "Billing"},
                                    {"field": "status", "op": "eq", "value": "escalated"},
                                    {"field": "opened_at", "op": "gte", "value": "2026-08-01"}]},
    {"metric": "count", "group_by": ["account"],
     "filters": [{"field": "has_duplicate", "op": "eq", "value": True}],
     "having": {"op": "gte", "value": 2}, "order_by": {"field": "value", "dir": "desc"}},
    {"metric": "avg_resolution_hours", "group_by": ["tier"],
     "filters": [{"field": "module", "op": "eq", "value": "API"}]},
    {"metric": "list", "filters": [{"field": "subject", "op": "contains", "value": "refund"}]},
    {"metric": "distinct_accounts", "filters": [{"field": "tier", "op": "in",
                                                 "value": ["gold", "platinum"]}]},
    {"metric": "duplicate_count", "group_by": ["module"]},
    {"metric": "count", "group_by": ["opened_month", "module"]},
]


@pytest.mark.parametrize("raw", _ALL_SHAPES)
def test_every_compiled_query_is_tenant_scoped_and_read_only(raw):
    spec = g.validate_spec(raw)
    cypher, params = g.compile_spec(spec, "TENANT-XYZ")

    # the tenant predicate is present, as a standalone WHERE filter
    assert "c.tenant_id = $tenant_id" in cypher
    assert params["tenant_id"] == "TENANT-XYZ"

    # the WHERE that carries the tenant filter is never directly preceded by
    # an OPTIONAL MATCH (which would absorb it into the pattern) — there is
    # always a `WITH` between them
    lines = [ln.strip() for ln in cypher.splitlines()]
    where_idx = next(i for i, ln in enumerate(lines)
                     if ln.startswith("WHERE c.tenant_id"))
    assert lines[where_idx - 1].startswith("WITH ")

    # no write / procedure keywords survive the compile
    g._assert_safe(cypher)
    assert not re.search(r"(?i)\b(create|merge|delete|set|remove|call)\b", cypher)


def test_account_join_carries_tenant_id_on_the_node_too():
    spec = g.validate_spec({"metric": "distinct_accounts"})
    cypher, _ = g.compile_spec(spec, "T1")
    assert "(a:Account {tenant_id: $tenant_id})" in cypher


def test_duplicate_subquery_is_tenant_scoped():
    spec = g.validate_spec({"metric": "count",
                            "filters": [{"field": "has_duplicate", "op": "eq", "value": True}]})
    cypher, _ = g.compile_spec(spec, "T1")
    assert "(:Case {tenant_id: $tenant_id})" in cypher


def test_filters_are_parameterised_not_inlined():
    spec = g.validate_spec({"metric": "count", "filters": [
        {"field": "status", "op": "eq", "value": "'; MATCH (n) DETACH DELETE n //"},
    ]})
    cypher, params = g.compile_spec(spec, "T1")
    assert "DETACH DELETE" not in cypher          # the value never touches the query text
    assert params["f0"] == "'; MATCH (n) DETACH DELETE n //"
    assert "c.status = $f0" in cypher


def test_list_metric_returns_case_columns_no_message_text():
    spec = g.validate_spec({"metric": "list"})
    cypher, _ = g.compile_spec(spec, "T1")
    assert "c.subject AS subject" in cypher
    assert "Message" not in cypher and "Reply" not in cypher and "mm.text" not in cypher


# ── _assert_safe ─────────────────────────────────────────────────────
def test_assert_safe_blocks_writes_and_unscoped():
    with pytest.raises(g.GraphQueryError):
        g._assert_safe("MATCH (c:Case) WHERE c.tenant_id = $tenant_id "
                       "SET c.x = 1 RETURN c")
    with pytest.raises(g.GraphQueryError):
        g._assert_safe("MATCH (c:Case) RETURN count(c) AS count")   # no tenant filter
    with pytest.raises(g.GraphQueryError):
        g._assert_safe("MATCH (c:Case) WHERE c.tenant_id = $tenant_id "
                       "CALL apoc.export.csv.all('x',{}) RETURN 1")


# ── to_spec (LLM boundary, monkeypatched) ────────────────────────────
def test_to_spec_parses_llm_json(monkeypatch):
    monkeypatch.setattr(g.llm, "available", lambda **k: True)
    monkeypatch.setattr(g.llm, "complete",
                        lambda *a, **k: '{"metric":"count","group_by":["module"]}')
    assert g.to_spec("how many by module", "T1") == {"metric": "count",
                                                     "group_by": ["module"]}


def test_to_spec_unsupported_question_raises(monkeypatch):
    monkeypatch.setattr(g.llm, "available", lambda **k: True)
    monkeypatch.setattr(g.llm, "complete", lambda *a, **k: '{"error":"unsupported"}')
    with pytest.raises(g.GraphQueryError):
        g.to_spec("what's the weather", "T1")


def test_to_spec_bad_json_raises(monkeypatch):
    monkeypatch.setattr(g.llm, "available", lambda **k: True)
    monkeypatch.setattr(g.llm, "complete", lambda *a, **k: "not json at all")
    with pytest.raises(g.GraphQueryError):
        g.to_spec("x", "T1")


def test_to_spec_no_llm_raises(monkeypatch):
    monkeypatch.setattr(g.llm, "available", lambda **k: False)
    with pytest.raises(g.GraphQueryError):
        g.to_spec("x", "T1")


# ── answer (end to end, driver monkeypatched) ────────────────────────
def test_answer_without_neo4j_configured_raises(monkeypatch):
    monkeypatch.delenv("NEO4J_URI", raising=False)
    with pytest.raises(g.GraphQueryError):
        g.answer("how many cases", "T1")


def test_answer_happy_path(monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "bolt://stub")
    monkeypatch.setattr(g, "to_spec",
                        lambda q, tid: {"metric": "count", "group_by": ["module"]})
    seen = {}

    def _fake_run(cypher, params):
        seen["cypher"] = cypher
        seen["params"] = params
        return ["g0", "value"], [{"g0": "Billing", "value": 4}, {"g0": "API", "value": 1}]

    monkeypatch.setattr(g, "_run", _fake_run)
    out = g.answer("cases by module", "TENANT-1")
    assert out["columns"] == ["g0", "value"]
    assert out["rows"][0] == {"g0": "Billing", "value": 4}
    assert out["truncated"] is False
    assert seen["params"]["tenant_id"] == "TENANT-1"
    assert "c.tenant_id = $tenant_id" in seen["cypher"]
