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


# --------------------------------------------------------------------------
# org introspection -- the field-mapping / dropdown foundation
# --------------------------------------------------------------------------
def test_redact_org_secret_keeps_only_the_safe_fields():
    creds = {"SF_USERNAME": "bot@acme.com", "SF_DOMAIN": "login",
             "SF_CONSUMER_KEY": "ck", "SF_PRIVATE_KEY": "-----BEGIN...-----"}
    r = salesforce.redact_org_secret(creds)
    assert r == {"SF_USERNAME": "bot@acme.com", "SF_DOMAIN": "login", "has_credentials": True}
    assert "SF_CONSUMER_KEY" not in r and "SF_PRIVATE_KEY" not in r


def test_redact_org_secret_no_secret_fields_present():
    assert salesforce.redact_org_secret({"SF_USERNAME": "x"})["has_credentials"] is False


def test_test_connection_reports_ok_and_failure(monkeypatch):
    class _OkClient:
        def query(self, soql):
            return {"records": [{"Id": "00D..."}]}

    monkeypatch.setattr(salesforce, "_build_client", lambda creds: _OkClient())
    assert salesforce.test_connection({"SF_USERNAME": "x"}) == {"ok": True, "error": None}

    def _boom(creds):
        raise RuntimeError("invalid session")

    monkeypatch.setattr(salesforce, "_build_client", _boom)
    r = salesforce.test_connection({"SF_USERNAME": "x"})
    assert r["ok"] is False and "invalid session" in r["error"]


class _FakeCaseDescribe:
    def describe(self):
        return {"fields": [
            {"name": "Module__c", "label": "Module", "type": "picklist", "custom": True,
             "picklistValues": [{"value": "Billing", "label": "Billing", "active": True},
                                {"value": "Retired", "label": "Retired", "active": False}]},
            {"name": "Priority", "label": "Priority", "type": "picklist", "custom": False,
             "picklistValues": [{"value": "High", "label": "High", "active": True}]},
            {"name": "Description", "label": "Description", "type": "textarea", "custom": False,
             "picklistValues": []},
            {"name": "Id", "label": "Case ID", "type": "id", "custom": False},  # not mappable
        ]}


class _FakeSFClient:
    Case = _FakeCaseDescribe()

    def query(self, soql):
        if "FROM User" in soql:
            return {"records": [
                {"Id": "005A", "Name": "Casey Lin", "Email": "casey@example.com"},
                {"Id": "005B", "Name": "Sam Rivera", "Email": "sam@example.com"},
            ]}
        assert "Group" in soql and "Queue" in soql
        return {"records": [
            {"Id": "00G1", "Name": "Billing Queue", "DeveloperName": "Billing_Queue"},
            {"Id": "00G2", "Name": "Support Queue", "DeveloperName": "Support_Queue"},
        ]}


def test_describe_case_fields_only_mappable_types_and_active_values(monkeypatch):
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _FakeSFClient())
    fields = salesforce.describe_case_fields("t1")
    names = {f["name"] for f in fields}
    assert names == {"Module__c", "Priority", "Description"}  # Id (type 'id') excluded
    module = next(f for f in fields if f["name"] == "Module__c")
    assert module["picklist_values"] == [{"value": "Billing", "label": "Billing"}]  # inactive dropped
    desc = next(f for f in fields if f["name"] == "Description")
    assert desc["picklist_values"] == []  # non-picklist type, still offered as a mapping target


def test_list_queues_shapes_the_real_org_queues(monkeypatch):
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _FakeSFClient())
    qs = salesforce.list_queues("t1")
    assert qs == [
        {"id": "00G1", "name": "Billing Queue", "developer_name": "Billing_Queue"},
        {"id": "00G2", "name": "Support Queue", "developer_name": "Support_Queue"},
    ]


def test_list_active_users_shapes_the_real_org_users(monkeypatch):
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _FakeSFClient())
    us = salesforce.list_active_users("t1")
    assert us == [
        {"id": "005A", "name": "Casey Lin", "email": "casey@example.com"},
        {"id": "005B", "name": "Sam Rivera", "email": "sam@example.com"},
    ]


def test_introspect_org_combines_both_and_degrades_per_section(monkeypatch):
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _FakeSFClient())
    out = salesforce.introspect_org("t1")
    assert (len(out["case_fields"]) == 3 and len(out["queues"]) == 2
            and len(out["users"]) == 2 and out["errors"] == [])

    def _broken_client(*a, **k):
        class _C:
            Case = type("D", (), {"describe": lambda self: (_ for _ in ()).throw(RuntimeError("boom"))})()
            def query(self, soql):
                raise RuntimeError("also boom")
        return _C()

    monkeypatch.setattr(salesforce, "client_for", _broken_client)
    out = salesforce.introspect_org("t1")
    assert out["case_fields"] == [] and out["queues"] == [] and out["users"] == []
    assert len(out["errors"]) == 3  # all three sections failed independently, none raised


# --------------------------------------------------------------------------
# org_metadata -- the legacy dropdown shape, now an adapter over
# introspect_org. Fixes a real bug: the old standalone implementation
# gated on available() (the *env* creds check), so a tenant with their
# own connected org and NO env creds at all always got available=False.
# --------------------------------------------------------------------------
def test_org_metadata_derives_legacy_fields_from_introspect_org(monkeypatch):
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _FakeSFClient())
    meta = salesforce.org_metadata("t1")
    assert meta["available"] is True
    assert meta["queues"] == [
        {"id": "00G1", "name": "Billing Queue", "developer_name": "Billing_Queue"},
        {"id": "00G2", "name": "Support Queue", "developer_name": "Support_Queue"},
    ]
    assert meta["case_types"] == []          # _FakeSFClient's Case has no "Type" field
    assert meta["case_fields"][0]["name"] == "Module__c"
    assert meta["users"] == [
        {"id": "005A", "name": "Casey Lin", "email": "casey@example.com"},
        {"id": "005B", "name": "Sam Rivera", "email": "sam@example.com"},
    ]


def test_org_metadata_available_even_with_no_env_creds_if_the_tenant_org_resolves(monkeypatch):
    """The bug this replaced: available() only ever checked env vars, so a
    tenant with their own connected org (no env creds at all) incorrectly
    got available=False. org_metadata must not repeat that."""
    for k in _HERMETIC:
        monkeypatch.delenv(k, raising=False)
    assert salesforce.available() is False   # confirms the env path really is empty
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _FakeSFClient())
    meta = salesforce.org_metadata("t1", "their-own-org")
    assert meta["available"] is True


def test_org_metadata_reports_an_error_only_when_both_sections_are_empty(monkeypatch):
    def _broken(*a, **k):
        raise RuntimeError("no creds anywhere")

    monkeypatch.setattr(salesforce, "client_for", _broken)
    meta = salesforce.org_metadata("t1")
    assert meta["available"] is False
    assert "error" in meta and "no creds anywhere" in meta["error"]


# --------------------------------------------------------------------------
# _try_client -- the same "available() is env-only, don't gate on it" bug
# turned out to affect every write/read function in this module, not just
# org_metadata (found 2026-09-03 during a robustness pass: a self-serve
# tenant with their own connected org and zero env creds would have every
# Salesforce write/read silently dry-run forever). Verify the fix directly.
# --------------------------------------------------------------------------
class _FakeWriteClient:
    """A minimal fake covering Case.update / CaseComment.create /
    FeedItem.create / query / restful -- enough to exercise each fixed
    function's real (non-dry-run) path."""

    def __init__(self):
        self.updates: list[tuple[str, dict]] = []

    class Case:
        @staticmethod
        def update(case_id, fields):
            return None

        @staticmethod
        def get(case_id):
            return {}

    class CaseComment:
        @staticmethod
        def create(rec):
            return {"id": "00a1"}

    def query(self, soql):
        return {"records": []}

    def restful(self, path, **kw):
        return {"id": "chatter1"}


def test_try_client_returns_none_when_genuinely_no_creds_anywhere(monkeypatch):
    """In production, client_for -> _client() -> _build_client({}) raises
    KeyError when there are truly no creds anywhere (no tenant row, no env
    creds); _try_client must catch that and return None rather than
    letting it propagate and kill the whole case run."""
    def _boom(*a, **k):
        raise KeyError("SF_USERNAME")

    monkeypatch.setattr(salesforce, "client_for", _boom)
    assert salesforce._try_client("t1") is None


def test_try_client_resolves_the_tenants_own_org_with_zero_env_creds(monkeypatch):
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _FakeWriteClient())
    assert salesforce._try_client("t1", "their-own-org") is not None


@pytest.mark.parametrize("fn,args,dry_run_key", [
    ("identify_sender", ("a@b.com",), "reason"),
    ("post_chatter", ("case1", "hello"), "dry_run"),
    ("add_case_comment", ("case1", "hello"), "dry_run"),
    ("log_email_message", ("case1",), "dry_run"),  # incoming= added in kwargs below
    ("assign_case", ("case1",), "dry_run"),
    ("send_case_reply", ("case1", "hello"), "dry_run"),
])
def test_write_functions_no_longer_gate_on_the_env_only_available_check(monkeypatch, fn, args, dry_run_key):
    """Each of these used to check `available()` (env-only) before ever
    trying the tenant's own connected org, so a self-serve tenant with no
    env creds always got the dry-run shape. With client_for resolving a
    real (fake) client, they must now attempt the real path instead."""
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _FakeWriteClient())
    kwargs = {"tenant_id": "t1", "org_label": "their-own-org"}
    if fn == "assign_case":
        kwargs["queue"] = "Support"  # assign_case no-ops with neither queue nor user_id
    if fn == "log_email_message":
        kwargs["incoming"] = True
    result = getattr(salesforce, fn)(*args, **kwargs)
    assert result.get(dry_run_key) not in (True, "salesforce not configured")


def test_update_case_fields_writes_for_a_tenant_with_no_env_creds(monkeypatch):
    fake = _FakeWriteClient()
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: fake)
    out = salesforce.update_case_fields("case1", {"Priority": "High"},
                                        tenant_id="t1", org_label="their-own-org")
    assert out["dry_run"] is False
    assert out["written"] == {"Priority": "High"}


def test_update_case_fields_degrades_instead_of_raising_on_a_transient_failure(monkeypatch):
    """Robustness-pass fix: this used to `raise` on any non-field error
    (rate limit, 5xx, timeout, expired session), killing the whole case
    run -- every sibling write function in this module is best-effort.
    A transient failure must now come back as a normal (non-raising)
    result with an `error` key, matching the others."""
    class _RateLimited(_FakeWriteClient):
        class Case:
            @staticmethod
            def update(case_id, fields):
                raise RuntimeError("REQUEST_LIMIT_EXCEEDED")

            @staticmethod
            def get(case_id):
                return {}

    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _RateLimited())
    out = salesforce.update_case_fields("case1", {"Priority": "High"}, tenant_id="t1")
    assert out["written"] == {}
    assert "REQUEST_LIMIT_EXCEEDED" in out["error"]


def test_ensure_case_dry_run_flag_reflects_the_tenants_own_client_not_env(monkeypatch):
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _FakeWriteClient())
    out = salesforce.ensure_case({"from": "a@business.com"}, tenant_id="t1", org_label="their-own-org")
    assert out["dry_run"] is False
