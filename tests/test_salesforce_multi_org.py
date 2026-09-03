"""
2026-09-03 -- multi-org-per-tenant Salesforce support. Scoped from a
multi-tenant audit: `tenant_integrations` used to have
`primary key (tenant_id, kind)`, so a tenant could hold at most one
Salesforce connection ever. Migration 082 widens that to
`(tenant_id, kind, org_label)`; `client_for()` gains an optional
`org_label` (default 'default', so every pre-existing call site that
only ever passed `tenant_id` keeps resolving exactly what it always
did).

Run:  pytest tests/test_salesforce_multi_org.py
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import salesforce

_HERMETIC = ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN", "SF_CONSUMER_KEY",
             "SF_CONSUMER_SECRET", "SF_PRIVATE_KEY", "SF_PRIVATE_KEY_FILE")


_ENV_SENTINEL = object()  # stands in for "the real env-configured client"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    for k in _HERMETIC:
        monkeypatch.delenv(k, raising=False)
    # _client() calls _build_client() unconditionally the first time (it's
    # not itself available()-gated -- callers are expected to check that);
    # pin it to a sentinel so tests never depend on this box's real .env.
    monkeypatch.setattr(salesforce, "_client_obj", _ENV_SENTINEL)
    salesforce._tenant_clients.clear()
    yield
    salesforce._tenant_clients.clear()


class _FakeTable:
    """A tiny fake of the Supabase query-builder chain, backed by a plain
    list of row dicts shared across a fake client -- same fake-DB shape
    used elsewhere in this test suite."""

    def __init__(self, rows: list[dict], name: str):
        self._all = rows
        self._name = name
        self._filters: dict[str, object] = {}
        self._pending_delete = False

    def select(self, *_a, **_k):
        return self

    def eq(self, k, v):
        self._filters[k] = v
        return self

    def order(self, *_a, **_k):
        return self

    def _matches(self, row):
        return all(row.get(k) == v for k, v in self._filters.items())

    def execute(self):
        # real Supabase clients defer everything to execute() -- .delete()
        # only marks intent; filters chained after it still apply.
        if self._pending_delete:
            to_drop = [r for r in self._all if self._matches(r)]
            self._all[:] = [r for r in self._all if r not in to_drop]
            return type("R", (), {"data": to_drop})
        return type("R", (), {"data": [r for r in self._all if self._matches(r)]})

    def upsert(self, row, on_conflict=None):
        key = (row["tenant_id"], row["kind"], row.get("org_label", "default"))
        self._all[:] = [r for r in self._all
                        if (r["tenant_id"], r["kind"], r.get("org_label", "default")) != key]
        self._all.append(row)
        return self

    def delete(self):
        self._pending_delete = True
        return self


class _FakeSB:
    def __init__(self):
        self.rows: list[dict] = []

    def table(self, name):
        assert name == "tenant_integrations"
        return _FakeTable(self.rows, name)


def test_client_for_defaults_to_the_default_org_and_falls_back_to_env(monkeypatch):
    """No stored creds at all -- every existing call site (`client_for(tid)`,
    no org_label) must keep getting the env-configured client, unchanged."""
    sb = _FakeSB()
    a = salesforce.client_for("t1", sb=sb)
    b = salesforce.client_for("t1", sb=sb)
    assert a is b  # cached
    assert a is _ENV_SENTINEL  # falls back to the env client


def test_client_for_resolves_different_orgs_to_different_clients(monkeypatch):
    captured = []

    def fake_build(creds):
        obj = object()
        captured.append((creds, obj))
        return obj

    monkeypatch.setattr(salesforce, "_build_client", fake_build)
    sb = _FakeSB()
    salesforce.save_tenant_org("t1", "prod", {"SF_USERNAME": "prod@acme.com"}, sb=sb)
    salesforce.save_tenant_org("t1", "sandbox", {"SF_USERNAME": "sandbox@acme.com"}, sb=sb)

    prod_client = salesforce.client_for("t1", "prod", sb=sb)
    sandbox_client = salesforce.client_for("t1", "sandbox", sb=sb)
    default_client = salesforce.client_for("t1", sb=sb)  # no org_label -> 'default', unset -> env

    assert prod_client is not sandbox_client
    assert default_client is _ENV_SENTINEL
    # each resolved with its own stored creds, not the other org's
    used = {c["SF_USERNAME"]: obj for c, obj in captured}
    assert used["prod@acme.com"] is prod_client
    assert used["sandbox@acme.com"] is sandbox_client


def test_client_for_caches_per_tenant_and_org(monkeypatch):
    calls = []
    monkeypatch.setattr(salesforce, "_build_client", lambda creds: calls.append(creds) or object())
    sb = _FakeSB()
    salesforce.save_tenant_org("t1", "prod", {"SF_USERNAME": "a"}, sb=sb)

    salesforce.client_for("t1", "prod", sb=sb)
    salesforce.client_for("t1", "prod", sb=sb)
    salesforce.client_for("t1", "prod", sb=sb)
    assert len(calls) == 1  # built once, cached after


def test_save_tenant_org_invalidates_the_cache(monkeypatch):
    monkeypatch.setattr(salesforce, "_build_client", lambda creds: creds["SF_USERNAME"])
    sb = _FakeSB()
    salesforce.save_tenant_org("t1", "prod", {"SF_USERNAME": "old"}, sb=sb)
    first = salesforce.client_for("t1", "prod", sb=sb)
    assert first == "old"

    salesforce.save_tenant_org("t1", "prod", {"SF_USERNAME": "new"}, sb=sb)
    second = salesforce.client_for("t1", "prod", sb=sb)
    assert second == "new"  # not the stale cached client


def test_list_tenant_orgs_and_delete(monkeypatch):
    monkeypatch.setattr(salesforce, "_build_client", lambda creds: object())
    sb = _FakeSB()
    assert salesforce.list_tenant_orgs("t1", sb=sb) == []

    salesforce.save_tenant_org("t1", "prod", {"SF_USERNAME": "a"}, sb=sb)
    salesforce.save_tenant_org("t1", "sandbox", {"SF_USERNAME": "b"}, sb=sb)
    assert salesforce.list_tenant_orgs("t1", sb=sb) == ["prod", "sandbox"]

    # a different tenant's orgs never leak into this list
    salesforce.save_tenant_org("t2", "prod", {"SF_USERNAME": "c"}, sb=sb)
    assert salesforce.list_tenant_orgs("t1", sb=sb) == ["prod", "sandbox"]

    salesforce.delete_tenant_org("t1", "sandbox", sb=sb)
    assert salesforce.list_tenant_orgs("t1", sb=sb) == ["prod"]
    # deleting doesn't touch client_for's cache for a DIFFERENT org
    salesforce.client_for("t1", "prod", sb=sb)  # still resolvable


def test_client_for_no_tenant_id_is_always_the_env_client():
    assert salesforce.client_for(None) is _ENV_SENTINEL
    assert salesforce.client_for(None, "prod") is _ENV_SENTINEL
