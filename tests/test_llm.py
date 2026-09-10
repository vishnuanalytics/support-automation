"""Billing chunk E (2026-09-10): `llm.last_key_source` — the "byok" vs
"platform" tag that lets billing exclude a tenant's own-key usage from
plan overage. Offline / mocked — `tests/test_llm_byok.py` covers the real
Vault round-trip for `_tenant_keys` itself; this covers the dispatch-level
wiring on top of it.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import llm


def test_is_byok_true_when_tenant_has_a_key_for_that_provider(monkeypatch):
    monkeypatch.setattr(llm, "_tenant_keys", lambda tid: {"groq": "tenant-key"})
    assert llm._is_byok("groq", "t1") is True


def test_is_byok_false_for_a_different_provider(monkeypatch):
    monkeypatch.setattr(llm, "_tenant_keys", lambda tid: {"groq": "tenant-key"})
    assert llm._is_byok("anthropic", "t1") is False


def test_is_byok_false_with_no_tenant_id(monkeypatch):
    monkeypatch.setattr(llm, "_tenant_keys", lambda tid: {"groq": "tenant-key"} if tid else {})
    assert llm._is_byok("groq", None) is False


def test_tenant_has_byok_true_iff_any_key_saved(monkeypatch):
    monkeypatch.setattr(llm, "_tenant_keys", lambda tid: {"anthropic": "x"})
    assert llm.tenant_has_byok("t1") is True
    monkeypatch.setattr(llm, "_tenant_keys", lambda tid: {})
    assert llm.tenant_has_byok("t1") is False


def test_dispatch_sets_platform_key_source_by_default(monkeypatch):
    monkeypatch.setattr(llm, "_tenant_keys", lambda tid: {})
    monkeypatch.setattr(llm, "_groq_complete", lambda *a, **k: "reply")
    llm._dispatch("openai/gpt-oss-120b", "sys", "user", 100, 0.2, False, tenant_id="t1")
    assert llm.last_key_source == "platform"


def test_dispatch_sets_byok_key_source_when_tenant_has_a_key(monkeypatch):
    monkeypatch.setattr(llm, "_tenant_keys", lambda tid: {"groq": "tenant-own-key"})
    captured = {}
    monkeypatch.setattr(llm, "_groq_complete",
                        lambda *a, api_key, **k: captured.update(api_key=api_key) or "reply")
    llm._dispatch("openai/gpt-oss-120b", "sys", "user", 100, 0.2, False, tenant_id="t1")
    assert llm.last_key_source == "byok"
    assert captured["api_key"] == "tenant-own-key"


def test_dispatch_no_tenant_id_is_always_platform(monkeypatch):
    monkeypatch.setattr(llm, "_groq_complete", lambda *a, **k: "reply")
    llm._dispatch("openai/gpt-oss-120b", "sys", "user", 100, 0.2, False, tenant_id=None)
    assert llm.last_key_source == "platform"


def test_dispatch_routes_anthropic_and_openrouter_the_same_way(monkeypatch):
    monkeypatch.setattr(llm, "_tenant_keys", lambda tid: {"anthropic": "tenant-key"})
    monkeypatch.setattr(llm, "_anthropic_complete", lambda *a, **k: "reply")
    llm._dispatch("claude-sonnet-5", "sys", "user", 100, 0.2, False, tenant_id="t1")
    assert llm.last_key_source == "byok"


def test_complete_resets_key_source_on_stub_fallback(monkeypatch):
    monkeypatch.setattr(llm, "_fallback_chain", lambda model, tenant_id=None: [])
    llm.last_key_source = "byok"   # simulate a stale value from a prior call
    llm.complete("sys", "user", model="openai/gpt-oss-120b")
    assert llm.last_key_source is None


def test_complete_resets_key_source_on_cache_hit(monkeypatch):
    monkeypatch.setattr(llm, "_CACHE_ON", True)
    ck = llm._ckey("openai/gpt-oss-120b", "sys", "user", 100)
    llm._cache[ck] = "cached reply"
    llm.last_key_source = "byok"   # simulate a stale value from a prior call
    out = llm.complete("sys", "user", model="openai/gpt-oss-120b", max_tokens=100, cache=True)
    assert out == "cached reply"
    assert llm.last_key_source is None
    del llm._cache[ck]


def test_complete_with_tools_sets_key_source_platform_by_default(monkeypatch):
    monkeypatch.setattr(llm, "_tenant_keys", lambda tid: {})
    monkeypatch.setattr(llm, "available", lambda model, tenant_id=None: True)

    class _R:
        text = "ok"
        tool_calls = []

    monkeypatch.setattr(llm, "_groq_complete_tools", lambda *a, **k: _R())
    llm.complete_with_tools(messages=[{"role": "user", "content": "hi"}], tools=[],
                            model="openai/gpt-oss-120b", tenant_id="t1")
    assert llm.last_key_source == "platform"


def test_complete_with_tools_sets_key_source_byok(monkeypatch):
    monkeypatch.setattr(llm, "_tenant_keys", lambda tid: {"anthropic": "tenant-key"})
    monkeypatch.setattr(llm, "available", lambda model, tenant_id=None: True)

    class _R:
        text = "ok"
        tool_calls = []

    monkeypatch.setattr(llm, "_anthropic_complete_tools", lambda *a, **k: _R())
    llm.complete_with_tools(messages=[{"role": "user", "content": "hi"}], tools=[],
                            model="claude-sonnet-5", tenant_id="t1")
    assert llm.last_key_source == "byok"


def test_complete_with_tools_unavailable_resets_key_source(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda model, tenant_id=None: False)
    llm.last_key_source = "byok"   # simulate a stale value from a prior call
    llm.complete_with_tools(messages=[{"role": "user", "content": "hi"}], tools=[],
                            model="openai/gpt-oss-120b", tenant_id="t1")
    assert llm.last_key_source is None
