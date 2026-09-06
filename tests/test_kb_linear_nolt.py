"""Linear (§4) + Nolt (§6) KB source connectors — the sync() producers and
their filters. Offline: the HTTP layer is mocked, no keys needed."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import kb_connectors, linear, nolt


def _ctx():
    return kb_connectors.SyncCtx(tenant_id="t", sb=None, collection_name="c")


# ── availability ─────────────────────────────────────────────────────────
def test_linear_available_reflects_a_stored_key(monkeypatch):
    monkeypatch.setattr("interpreter.vault_secrets.get", lambda *a, **k: {})
    assert linear.available("t", None)[0] is False
    monkeypatch.setattr("interpreter.vault_secrets.get", lambda *a, **k: {"api_key": "lin_x"})
    assert linear.available("t", None) == (True, None)


def test_nolt_available_reflects_a_stored_key(monkeypatch):
    monkeypatch.setattr("interpreter.vault_secrets.get", lambda *a, **k: {})
    assert nolt.available("t", None)[0] is False
    monkeypatch.setattr("interpreter.vault_secrets.get", lambda *a, **k: {"api_key": "k"})
    assert nolt.available("t", None) == (True, None)


# ── normalize ────────────────────────────────────────────────────────────
def test_norm_linear():
    spec = kb_connectors.get_kb_connector("linear")
    assert spec.normalize_config({}) == {"include": "both"}
    assert spec.normalize_config({"include": "issues", "team_key": "ENG"}) == {
        "include": "issues", "team_key": "ENG"}
    with pytest.raises(ValueError):
        spec.normalize_config({"include": "bogus"})


def test_norm_nolt_requires_board_id():
    spec = kb_connectors.get_kb_connector("nolt")
    assert spec.normalize_config({"board_id": " b123 "}) == {"board_id": "b123"}
    with pytest.raises(ValueError):
        spec.normalize_config({})


def test_apikey_fields_are_marked_secret():
    for slug in ("linear", "nolt"):
        f = {x["key"]: x for x in kb_connectors.get_kb_connector(slug).config_fields}
        assert f["api_key"]["secret"] is True and not f["api_key"].get("required")


# ── _sync_linear ─────────────────────────────────────────────────────────
def test_sync_linear_both_documents_and_resolved_issues(monkeypatch):
    monkeypatch.setattr(linear, "fetch_documents", lambda t, s: [
        {"id": "D1", "title": "Runbook", "content": "# Runbook\nsteps", "updatedAt": "u", "url": "http://l/d1"},
        {"id": "D2", "title": "Empty", "content": "   ", "updatedAt": "u", "url": "x"},  # skipped
    ])
    monkeypatch.setattr(linear, "fetch_resolved_issues", lambda t, s, *, team_key=None: [
        {"id": "I1", "identifier": "ENG-1", "title": "Proxy timeout", "description": "root cause: pool",
         "url": "http://l/i1", "updatedAt": "u",
         "comments": {"nodes": [{"body": "fixed by raising the pool", "user": {"name": "Ana"}}]}},
        {"id": "I2", "identifier": "ENG-2", "title": "wontfix", "description": "",
         "url": "x", "updatedAt": "u", "comments": {"nodes": []}},   # skipped: no body
    ])
    res = kb_connectors._sync_linear({"include": "both"}, None, _ctx())
    ids = [d.external_id for d in res.documents]
    assert ids == ["doc:D1", "issue:I1"]
    assert res.documents[0].origin == "linear" and res.documents[0].quality == "official"
    assert res.documents[1].quality == "community_resolved"
    assert "**Ana:** fixed by raising the pool" in res.documents[1].body_md
    assert res.documents[0].body_md.startswith("<!-- http://l/d1 -->")


def test_sync_linear_include_documents_only(monkeypatch):
    monkeypatch.setattr(linear, "fetch_documents", lambda t, s: [
        {"id": "D1", "title": "R", "content": "x", "updatedAt": "u", "url": ""}])
    called = []
    monkeypatch.setattr(linear, "fetch_resolved_issues",
                        lambda *a, **k: called.append(1) or [])
    res = kb_connectors._sync_linear({"include": "documents"}, None, _ctx())
    assert [d.external_id for d in res.documents] == ["doc:D1"] and called == []


# ── nolt ─────────────────────────────────────────────────────────────────
def test_nolt_is_resolved_matches_common_statuses():
    assert nolt._is_resolved({"status": {"title": "Done"}}) is True
    assert nolt._is_resolved({"status": {"title": "shipped"}}) is True
    assert nolt._is_resolved({"status": {"title": "Planned"}}) is False
    assert nolt._is_resolved({"status": None}) is False


def test_sync_nolt_builds_a_doc_per_resolved_post(monkeypatch):
    monkeypatch.setattr(nolt, "fetch_resolved_posts", lambda t, s, bid, **k: [
        {"id": "P1", "title": "Dark mode", "description": "shipped in 2.1", "url": "http://n/p1",
         "updatedAt": "u",
         "_comments": [{"body": "rolled out to everyone", "author": {"name": "Ops"}}]},
    ])
    res = kb_connectors._sync_nolt({"board_id": "b1"}, None, _ctx())
    d = res.documents[0]
    assert d.external_id == "post:P1" and d.origin == "nolt" and d.quality == "community_resolved"
    assert d.body_md.startswith("<!-- http://n/p1 -->")
    assert "**Ops:** rolled out to everyone" in d.body_md


def test_registry_lists_linear_and_nolt():
    slugs = {s.slug for s in kb_connectors.list_kb_connectors()}
    assert {"linear", "nolt"} <= slugs
