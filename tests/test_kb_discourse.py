"""Discourse KB source connector (docs/KB_SOURCE_CONNECTORS.md §5).
Offline: the HTTP layer is mocked."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import discourse, kb_connectors


def _ctx():
    return kb_connectors.SyncCtx(tenant_id="t", sb=None, collection_name="c")


def test_norm_discourse():
    spec = kb_connectors.get_kb_connector("discourse")
    assert spec.normalize_config({"base_url": "https://f.acme.com/"}) == {
        "base_url": "https://f.acme.com", "resolved_only": True, "max_items": 300}
    cfg = spec.normalize_config({"base_url": "https://f.acme.com", "resolved_only": "no",
                                 "category": "support", "api_username": "bot", "max_items": 20})
    assert cfg == {"base_url": "https://f.acme.com", "resolved_only": False,
                   "category": "support", "api_username": "bot", "max_items": 20}
    with pytest.raises(ValueError):
        spec.normalize_config({"base_url": "forum.acme.com"})   # no scheme
    with pytest.raises(ValueError):
        spec.normalize_config({})


def test_discourse_available_is_always_true():
    assert kb_connectors.get_kb_connector("discourse").is_available("t", None) == (True, None)


def test_api_key_field_is_secret_and_optional():
    f = {x["key"]: x for x in kb_connectors.get_kb_connector("discourse").config_fields}
    assert f["api_key"]["secret"] is True and not f["api_key"].get("required")


def _fake_forum(monkeypatch, topics, per_topic):
    """topics = the /latest topic_list; per_topic = {id: [posts]}."""
    def _get(tid, sb, base, path, **kw):
        if path.startswith("/latest") or path.startswith("/c/"):
            # one page then empty
            return {"topic_list": {"topics": topics if "page=0" in path else []}}
        if path.startswith("/t/"):
            tid_ = int(path.split("/t/")[1].split(".")[0])
            return {"post_stream": {"posts": per_topic.get(tid_, [])}}
        if path == "/site.json":
            return {"title": "Acme Forum"}
        return {}
    monkeypatch.setattr(discourse, "_get", _get)


def test_sync_discourse_resolved_only_keeps_solved_threads(monkeypatch):
    _fake_forum(
        monkeypatch,
        topics=[{"id": 1, "title": "Webhook 500s", "has_accepted_answer": True},
                {"id": 2, "title": "Just chatting", "has_accepted_answer": False}],
        per_topic={1: [{"username": "asker", "raw": "our webhook 500s", "accepted_answer": False},
                       {"username": "mod", "raw": "raise the timeout to 30s", "accepted_answer": True}]},
    )
    res = kb_connectors._sync_discourse(
        {"base_url": "https://f.x", "resolved_only": True, "max_items": 300}, None, _ctx())
    assert [d.external_id for d in res.documents] == ["topic:1"]      # #2 skipped (unsolved)
    d = res.documents[0]
    assert d.origin == "discourse" and d.quality == "community_resolved"
    assert "**Question (asker):** our webhook 500s" in d.body_md
    assert "**Accepted answer (mod):** raise the timeout to 30s" in d.body_md
    assert res.exhaustive is False                                   # /latest is a window


def test_sync_discourse_resolved_only_no_keeps_unsolved_as_unverified(monkeypatch):
    _fake_forum(
        monkeypatch,
        topics=[{"id": 5, "title": "Open question", "has_accepted_answer": False}],
        per_topic={5: [{"username": "a", "raw": "how do I X?", "accepted_answer": False},
                       {"username": "b", "raw": "maybe try Y", "accepted_answer": False},
                       {"username": "c", "raw": "or Z", "accepted_answer": False}]},
    )
    res = kb_connectors._sync_discourse(
        {"base_url": "https://f.x", "resolved_only": False, "max_items": 300}, None, _ctx())
    d = res.documents[0]
    assert d.quality == "unverified"
    assert "**b:** maybe try Y" in d.body_md and "**c:** or Z" in d.body_md   # context replies


def test_discourse_test_connection(monkeypatch):
    _fake_forum(monkeypatch, topics=[], per_topic={})
    assert discourse.test_connection("t", None, "https://f.x")["ok"] is True
    def boom(*a, **k):
        raise RuntimeError("Discourse API 403")
    monkeypatch.setattr(discourse, "_get", boom)
    assert discourse.test_connection("t", None, "https://f.x")["ok"] is False


def test_registry_lists_discourse():
    assert "discourse" in {s.slug for s in kb_connectors.list_kb_connectors()}
