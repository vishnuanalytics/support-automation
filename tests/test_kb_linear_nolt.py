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
    assert spec.normalize_config({}) == {"include": "both", "max_items": 300}
    assert spec.normalize_config({"include": "issues", "team_key": "ENG", "max_items": 50}) == {
        "include": "issues", "team_key": "ENG", "max_items": 50}
    # out-of-range / garbage clamps to the default range
    assert spec.normalize_config({"max_items": 99999})["max_items"] == 2000
    assert spec.normalize_config({"max_items": "abc"})["max_items"] == 300
    with pytest.raises(ValueError):
        spec.normalize_config({"include": "bogus"})


def test_norm_nolt_requires_board_id():
    spec = kb_connectors.get_kb_connector("nolt")
    assert spec.normalize_config({"board_id": " b123 "}) == {"board_id": "b123", "max_items": 500}
    with pytest.raises(ValueError):
        spec.normalize_config({})


def test_apikey_fields_are_marked_secret():
    for slug in ("linear", "nolt"):
        f = {x["key"]: x for x in kb_connectors.get_kb_connector(slug).config_fields}
        assert f["api_key"]["secret"] is True and not f["api_key"].get("required")


# ── _sync_linear ─────────────────────────────────────────────────────────
def test_sync_linear_both_documents_and_resolved_issues(monkeypatch):
    monkeypatch.setattr(linear, "fetch_documents", lambda t, s, *, limit=None, updated_after=None: [
        {"id": "D1", "title": "Runbook", "content": "# Runbook\nsteps", "updatedAt": "u", "url": "http://l/d1"},
        {"id": "D2", "title": "Empty", "content": "   ", "updatedAt": "u", "url": "x"},  # skipped
    ])
    monkeypatch.setattr(linear, "fetch_resolved_issues", lambda t, s, *, team_key=None, limit=None, updated_after=None: [
        {"id": "I1", "identifier": "ENG-1", "title": "Proxy timeout", "description": "root cause: pool",
         "url": "http://l/i1", "updatedAt": "u",
         "comments": {"nodes": [{"body": "fixed by raising the pool", "user": {"name": "Ana"}}]}},
        {"id": "I2", "identifier": "ENG-2", "title": "wontfix", "description": "",
         "url": "x", "updatedAt": "u", "comments": {"nodes": []}},   # skipped: no body
    ])
    res = kb_connectors._sync_linear({"include": "both", "max_items": 300}, None, _ctx())
    ids = [d.external_id for d in res.documents]
    assert ids == ["doc:D1", "issue:I1"]
    assert res.documents[0].origin == "linear" and res.documents[0].quality == "official"
    assert res.documents[1].quality == "community_resolved"
    assert "**Ana:** fixed by raising the pool" in res.documents[1].body_md
    assert res.documents[0].body_md.startswith("<!-- http://l/d1 -->")


def test_sync_linear_include_documents_only(monkeypatch):
    monkeypatch.setattr(linear, "fetch_documents", lambda t, s, *, limit=None, updated_after=None: [
        {"id": "D1", "title": "R", "content": "x", "updatedAt": "u", "url": ""}])
    called = []
    monkeypatch.setattr(linear, "fetch_resolved_issues",
                        lambda *a, **k: called.append(1) or [])
    res = kb_connectors._sync_linear({"include": "documents"}, None, _ctx())
    assert [d.external_id for d in res.documents] == ["doc:D1"] and called == []


def test_sync_linear_max_items_is_passed_down_and_marks_not_exhaustive(monkeypatch):
    seen = {}
    monkeypatch.setattr(linear, "fetch_documents",
                        lambda t, s, *, limit=None, updated_after=None: seen.update(docs_after=updated_after) or seen.__setitem__("docs_limit", limit) or
                        [{"id": f"D{i}", "title": "d", "content": "x", "url": ""} for i in range(2)])
    monkeypatch.setattr(linear, "fetch_resolved_issues",
                        lambda t, s, *, team_key=None, limit=None, updated_after=None:
                        seen.__setitem__("iss_limit", limit) or [])
    res = kb_connectors._sync_linear({"include": "both", "max_items": 2}, None, _ctx())
    assert seen["docs_limit"] == 2 and seen["iss_limit"] == 2
    assert len(res.documents) == 2 and res.exhaustive is False   # hit the cap


def test_linear_page_stops_at_the_limit(monkeypatch):
    pages = [
        {"nodes": [{"id": "1"}, {"id": "2"}], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}},
        {"nodes": [{"id": "3"}, {"id": "4"}], "pageInfo": {"hasNextPage": True, "endCursor": "c2"}},
    ]
    calls = []
    monkeypatch.setattr(linear, "_gql",
                        lambda t, s, q, v: calls.append(1) or {"k": pages[len(calls) - 1]})
    out = linear._page("t", None, "q", "k", {}, limit=3)
    assert [n["id"] for n in out] == ["1", "2", "3"]
    assert len(calls) == 2   # stopped after the 2nd page, didn't fetch a 3rd


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


# ── test_connection ─────────────────────────────────────────────────────
def test_linear_test_connection_ok(monkeypatch):
    monkeypatch.setattr(linear, "_gql",
                        lambda t, s, q, **k: {"viewer": {"id": "u1", "name": "Ada"}})
    assert linear.test_connection("t", None) == {"ok": True, "detail": "connected as Ada"}


def test_linear_test_connection_passes_the_override_key(monkeypatch):
    seen = {}
    monkeypatch.setattr(linear, "_gql",
                        lambda t, s, q, **k: seen.update(k) or {"viewer": {"name": "x"}})
    linear.test_connection("t", None, api_key="lin_OVERRIDE")
    assert seen["api_key"] == "lin_OVERRIDE"


def test_linear_test_connection_reports_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("Linear API 401: bad key")
    monkeypatch.setattr(linear, "_gql", boom)
    r = linear.test_connection("t", None)
    assert r["ok"] is False and "401" in r["detail"]


def test_nolt_test_connection(monkeypatch):
    monkeypatch.setattr(nolt, "_get", lambda *a, **k: [])
    assert nolt.test_connection("t", None, "b1") == {"ok": True, "detail": "board reachable"}
    def boom(*a, **k):
        raise RuntimeError("Nolt API 403")
    monkeypatch.setattr(nolt, "_get", boom)
    assert nolt.test_connection("t", None, "b1")["ok"] is False


# ── incremental (watermark) ────────────────────────────────────────────
def test_sync_linear_incremental_uses_updated_after_and_does_not_archive(monkeypatch):
    seen = {}
    monkeypatch.setattr(linear, "fetch_documents",
                        lambda t, s, *, limit=None, updated_after=None:
                        seen.__setitem__("d_after", updated_after) or
                        [{"id": "D9", "title": "d", "content": "x", "url": "", "updatedAt": "2026-02-02"}])
    monkeypatch.setattr(linear, "fetch_resolved_issues",
                        lambda t, s, *, team_key=None, limit=None, updated_after=None:
                        seen.__setitem__("i_after", updated_after) or [])
    res = kb_connectors._sync_linear(
        {"include": "both", "max_items": 300}, {"since": "2026-01-01"}, _ctx())
    assert seen["d_after"] == "2026-01-01" and seen["i_after"] == "2026-01-01"
    assert res.exhaustive is False                      # incremental -> never archive
    assert res.watermark["since"] == "2026-02-02"       # advanced to the newest seen


def test_sync_linear_first_run_is_full_and_records_a_since(monkeypatch):
    monkeypatch.setattr(linear, "fetch_documents",
                        lambda t, s, *, limit=None, updated_after=None:
                        [{"id": "D1", "title": "d", "content": "x", "url": "", "updatedAt": "2026-03-03"}])
    monkeypatch.setattr(linear, "fetch_resolved_issues",
                        lambda *a, **k: [])
    res = kb_connectors._sync_linear({"include": "both", "max_items": 300}, None, _ctx())
    assert res.exhaustive is True                       # first run: full, may archive
    assert res.watermark["since"] == "2026-03-03"


def test_gdocs_folder_reuses_a_stored_body_when_modifiedtime_matches(monkeypatch):
    monkeypatch.setattr("interpreter.gdrive.list_folder_docs",
                        lambda *a, **k: [{"id": "d1", "name": "One", "modified_time": "M1"},
                                         {"id": "d2", "name": "Two", "modified_time": "M2-new"}])
    fetched = []
    monkeypatch.setattr("interpreter.gdrive.fetch_doc",
                        lambda tid, did, sb: fetched.append(did) or
                        {"title": did, "markdown": f"fresh {did}", "modified_time": "M2-new"})
    ctx = kb_connectors.SyncCtx(
        tenant_id="t", sb=None, collection_name="c",
        existing={"d1": {"body_md": "stored d1", "gdoc_modified": "M1"},
                  "d2": {"body_md": "stored d2", "gdoc_modified": "M2-old"}})
    res = kb_connectors._sync_gdocs({"folder_id": "F", "index": True}, None, ctx)
    assert fetched == ["d2"]                            # d1 reused, only d2 re-fetched
    by_id = {d.external_id: d.body_md for d in res.documents}
    assert by_id["d1"] == "stored d1" and by_id["d2"] == "fresh d2"
    assert res.watermark["reused"] == 1
