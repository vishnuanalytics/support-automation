"""
2026-09-03 -- robustness pass. Two related bugs found by code review while
building a multi-tenant concurrency stress test:

1. `queue_member`/`_sf_team_member`/`_sf_queue_id` (interpreter/routing.py)
   and `_intake_queue_id` (interpreter/salesforce.py) gated on
   `salesforce.available()` -- the *env-only* creds check -- before ever
   trying `client_for(tenant_id, org_label)`, so a self-serve tenant with
   their own connected org and zero env creds always got `(None, None)`.
   Same bug class as the one already fixed in salesforce.py's 12
   write/read functions two chunks earlier; this file covers the routing
   module's instances of it.
2. `queue_member`'s cache key didn't include `tenant_id` at all (only
   `queue_ref` + `org_label`) and `_intake_queue_id`'s cache key was the
   single constant queue name -- two tenants provisioning identically-
   named Salesforce queues (which scripts/sf_support_setup.py has every
   tenant do) would have the SECOND tenant's lookup silently return the
   FIRST tenant's cached (wrong-org) Group id.

Run:  pytest tests/test_routing_tenant_scoping.py
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import routing, salesforce

_HERMETIC = ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN", "SF_CONSUMER_KEY",
             "SF_CONSUMER_SECRET", "SF_PRIVATE_KEY", "SF_PRIVATE_KEY_FILE")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    for k in _HERMETIC:
        monkeypatch.delenv(k, raising=False)
    routing._cache.clear()
    salesforce._intake_queue_cache.clear()
    yield
    routing._cache.clear()
    salesforce._intake_queue_cache.clear()


class _FakeSF:
    """A queue "Support_Queue" whose member differs per fake client instance
    -- stands in for two tenants' orgs each having a same-named queue with
    a genuinely different member/id."""

    def __init__(self, user_id: str, user_name: str, group_id: str = "00G1"):
        self.user_id, self.user_name, self.group_id = user_id, user_name, group_id

    def query(self, soql):
        if "FROM User" in soql:
            return {"records": [{"Id": self.user_id, "Name": self.user_name}]}
        if "FROM Group" in soql:
            return {"records": [{"Id": self.group_id}]}
        return {"records": []}


def test_queue_member_no_longer_gates_on_the_env_only_available_check(monkeypatch):
    assert salesforce.available() is False
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _FakeSF("005A", "Alice"))
    uid, name = routing.queue_member("Support_Queue", tenant_id="t1", org_label="their-org")
    assert (uid, name) == ("005A", "Alice")


def test_queue_member_cache_does_not_cross_tenants(monkeypatch):
    """The real bug: same queue_ref, two different tenants, two different
    real members -- tenant B must never see tenant A's cached member."""
    clients = {"t1": _FakeSF("005A", "Alice"), "t2": _FakeSF("005B", "Bob")}
    monkeypatch.setattr(salesforce, "client_for", lambda tid, org=None, sb=None: clients[tid])

    a = routing.queue_member("Support_Queue", tenant_id="t1", org_label="default")
    b = routing.queue_member("Support_Queue", tenant_id="t2", org_label="default")
    assert a == ("005A", "Alice")
    assert b == ("005B", "Bob")
    assert a != b  # the bug this replaces would have made these equal


def test_sf_team_member_no_longer_gates_on_available(monkeypatch):
    assert salesforce.available() is False
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _FakeSF("005A", "Alice"))
    uid, name = routing._sf_team_member("csm", "t1", "their-org")
    assert (uid, name) == ("005A", "Alice")


def test_sf_queue_id_no_longer_gates_on_available(monkeypatch):
    assert salesforce.available() is False
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _FakeSF("005A", "Alice", "00G9"))
    qid = routing._sf_queue_id("Support_Queue", "t1", "their-org")
    assert qid == "00G9"


def test_intake_queue_id_is_scoped_per_tenant_not_by_queue_name_alone():
    a = salesforce._intake_queue_id(_FakeSF("_", "_", "00G_A"), "t1", "default")
    b = salesforce._intake_queue_id(_FakeSF("_", "_", "00G_B"), "t2", "default")
    assert a == "00G_A"
    assert b == "00G_B"
    assert a != b  # the bug this replaces would have made these equal
