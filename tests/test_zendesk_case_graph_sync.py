"""Phase 31 chunk 3 — ingestion/zendesk_case_graph_sync.py: Zendesk tickets
-> the Neo4j case-lifecycle graph (parity with the Salesforce-only
case_graph_sync). No network, no Neo4j; the Zendesk client and
case_memory.sync_case_lifecycle are faked."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ingestion import zendesk_case_graph_sync as zcgs
from interpreter import case_memory, zendesk


class _FakeZC:
    """Dispatches request() on the path; `pages` feeds /incremental/tickets."""

    def __init__(self, *, pages=None, comments=None, users=None, orgs=None, groups=None):
        self._pages = list(pages or [])
        self._comments = comments or {}
        self._users = users or {}
        self._orgs = orgs or {}
        self._groups = groups or []
        self.calls: list[str] = []

    def request(self, method, path, *, json=None, params=None):
        self.calls.append(path)
        if path == "/incremental/tickets.json":
            return self._pages.pop(0) if self._pages else {"tickets": [], "end_of_stream": True}
        if path.startswith("/tickets/") and path.endswith("/comments.json"):
            tid = int(path.split("/")[2])
            return {"comments": self._comments.get(tid, [])}
        if path.startswith("/users/"):
            uid = int(path.split("/")[2].split(".")[0])
            return {"user": self._users.get(uid, {})}
        if path.startswith("/organizations/"):
            oid = int(path.split("/")[2].split(".")[0])
            return {"organization": self._orgs.get(oid, {})}
        if path == "/groups.json":
            return {"groups": self._groups}
        return {}


def _patch(monkeypatch, zc, *, tenants=("T1",)):
    monkeypatch.setattr(zendesk, "_client", lambda tid, sb=None: zc)
    monkeypatch.setattr(zendesk, "active_connector_tenants",
                        lambda sb, only=None: [t for t in tenants if only in (None, t)])
    monkeypatch.setattr(zcgs, "_sb", lambda: object())
    monkeypatch.setattr(zcgs, "_load_state", lambda sb, scope: {})
    saved = []
    monkeypatch.setattr(zcgs, "_save_state",
                        lambda *a, **k: saved.append((a, k)))
    calls = []
    monkeypatch.setattr(case_memory, "sync_case_lifecycle",
                        lambda row, msgs: calls.append((row, msgs)) or True)
    return calls, saved


# ── helpers ────────────────────────────────────────────────────────
def test_iso_to_unix():
    assert zcgs._iso_to_unix("1970-01-01T00:00:00Z") == 0
    assert zcgs._iso_to_unix("2026-01-01T00:00:00+00:00") == 1767225600
    assert zcgs._iso_to_unix(None) == 0
    assert zcgs._iso_to_unix("garbage") == 0


def test_incremental_pages_until_end_of_stream():
    zc = _FakeZC(pages=[
        {"tickets": [{"id": 1}], "end_time": 100, "end_of_stream": False},
        {"tickets": [{"id": 2}], "end_time": 200, "end_of_stream": True},
    ])
    got = zcgs._incremental_tickets(zc, 0, 5000)
    assert [t["id"] for t in got] == [1, 2]


def test_incremental_respects_limit():
    zc = _FakeZC(pages=[{"tickets": [{"id": i} for i in range(10)],
                         "end_time": 1, "end_of_stream": False}])
    assert len(zcgs._incremental_tickets(zc, 0, 3)) == 3


def test_cache_fetches_each_id_once():
    zc = _FakeZC(users={5: {"email": "a@x.com", "role": "end-user"}},
                 orgs={9: {"domain_names": ["x.com"]}},
                 groups=[{"id": 7, "name": "Tier 2"}])
    c = zcgs._Cache(zc)
    assert c.user(5)["email"] == "a@x.com"
    assert c.user(5)["email"] == "a@x.com"
    assert c.org(9)["domain_names"] == ["x.com"]
    assert c.group_name(7) == "Tier 2"
    assert c.group_name(7) == "Tier 2"
    assert zc.calls.count("/users/5.json") == 1
    assert zc.calls.count("/groups.json") == 1


# ── message extraction ─────────────────────────────────────────────
def test_ticket_messages_roles_and_order():
    zc = _FakeZC(
        comments={1: [
            {"id": 10, "author_id": 5, "public": True, "body": "help me",
             "created_at": "2026-09-01T09:00:00Z"},
            {"id": 11, "author_id": 8, "public": False, "body": "internal check",
             "created_at": "2026-09-01T10:00:00Z"},
            {"id": 12, "author_id": 8, "public": False,
             "body": "[bot draft] proposed reply", "created_at": "2026-09-01T10:30:00Z"},
            {"id": 13, "author_id": 8, "public": True, "body": "here is the fix",
             "created_at": "2026-09-01T11:00:00Z"},
            {"id": 14, "author_id": 8, "public": True, "body": "   ",
             "created_at": "2026-09-01T12:00:00Z"},
        ]},
        users={8: {"role": "agent"}})
    cache = zcgs._Cache(zc)
    msgs = zcgs._ticket_messages(zc, {"id": 1, "requester_id": 5}, cache)
    by = {m["id"]: m for m in msgs}
    assert [m["id"] for m in msgs] == ["10", "11", "12", "13"]   # blank dropped, sorted
    assert by["10"]["role"] == "inbound" and by["10"]["author_kind"] == "customer"
    assert by["11"]["role"] == "agent_note" and by["11"]["author_kind"] == "agent"
    assert by["12"]["role"] == "draft" and by["12"]["author_kind"] == "bot"
    assert by["13"]["role"] == "agent_reply" and by["13"]["author_kind"] == "agent"


def test_ticket_messages_end_user_role_counts_as_customer():
    zc = _FakeZC(comments={1: [
        {"id": 20, "author_id": 99, "public": True, "body": "a colleague writes in",
         "created_at": "2026-09-01T09:00:00Z"}]},
        users={99: {"role": "end-user"}})
    msgs = zcgs._ticket_messages(zc, {"id": 1, "requester_id": 5}, zcgs._Cache(zc))
    assert msgs[0]["role"] == "inbound" and msgs[0]["author_kind"] == "customer"


# ── row normalisation ──────────────────────────────────────────────
def test_ticket_row_maps_zendesk_fields():
    zc = _FakeZC(users={5: {"email": "Dana@Acme.com"}},
                 orgs={9: {"domain_names": ["Acme.com", "acme.io"]}},
                 groups=[{"id": 7, "name": "Support Tier 2"}])
    cache = zcgs._Cache(zc)
    row = zcgs._ticket_row({
        "id": 42, "subject": "Login broken", "status": "solved", "type": "incident",
        "created_at": "2026-09-01T09:00:00Z", "updated_at": "2026-09-02T15:00:00Z",
        "requester_id": 5, "organization_id": 9, "group_id": 7,
        "via": {"channel": "email"},
    }, "T1", cache)
    assert row["sf_id"] == "42" and row["case_number"] == "42"
    assert row["is_closed"] is True
    assert row["closed_at"] == "2026-09-02T15:00:00Z"     # updated_at stands in
    assert row["case_type"] == "incident" and row["module"] is None
    assert row["routed_team"] == "Support Tier 2" and row["origin"] == "email"
    assert row["account_id"] == "9" and row["account_domain"] == "acme.com"
    assert row["contact_email"] == "dana@acme.com"


def test_ticket_row_open_ticket_has_no_closed_at():
    zc = _FakeZC()
    row = zcgs._ticket_row({"id": 1, "status": "open", "updated_at": "2026-09-02T00:00:00Z"},
                           "T1", zcgs._Cache(zc))
    assert row["is_closed"] is False and row["closed_at"] is None


# ── sync ───────────────────────────────────────────────────────────
def test_sync_tenant_merges_each_ticket_and_checkpoints(monkeypatch):
    zc = _FakeZC(
        pages=[{"tickets": [
            {"id": 1, "subject": "a", "status": "open", "requester_id": 5,
             "updated_at": "2026-09-01T00:00:00Z"},
            {"id": 2, "subject": "b", "status": "new", "requester_id": 5,
             "updated_at": "2026-09-03T00:00:00Z"},
        ], "end_of_stream": True}],
        comments={1: [{"id": 9, "author_id": 5, "public": True, "body": "hi",
                       "created_at": "2026-09-01T00:00:00Z"}], 2: []},
        users={5: {"email": "u@x.com"}})
    calls, saved = _patch(monkeypatch, zc)
    c, m = zcgs._sync_tenant(object(), "T1", since=None, limit=100, dry=False)
    assert c == 2 and m == 1
    assert [row["sf_id"] for row, _ in calls] == ["1", "2"]
    assert saved and saved[-1][1]["high_water"] == "2026-09-03T00:00:00Z"


def test_sync_tenant_dry_run_calls_no_merge(monkeypatch):
    zc = _FakeZC(pages=[{"tickets": [{"id": 1, "status": "new", "requester_id": 5,
                                      "updated_at": "2026-09-01T00:00:00Z"}],
                         "end_of_stream": True}], comments={1: []})
    calls, saved = _patch(monkeypatch, zc)
    c, m = zcgs._sync_tenant(object(), "T1", since=None, limit=100, dry=True)
    assert c == 1 and calls == [] and saved == []


def test_sync_iterates_tenants_and_isolates_a_failure(monkeypatch):
    zc = _FakeZC()
    _patch(monkeypatch, zc, tenants=("OK", "BAD"))

    def _one(sb, tid, *, since, limit, dry):
        if tid == "BAD":
            raise RuntimeError("zendesk 500")
        return (1, 1)

    monkeypatch.setattr(zcgs, "_sync_tenant", _one)
    assert zcgs.sync(dry=True) == 0     # BAD swallowed, still returns 0


def test_sync_noop_without_zendesk_tenants(monkeypatch):
    monkeypatch.setattr(zcgs, "_sb", lambda: object())
    monkeypatch.setattr(zendesk, "active_connector_tenants", lambda sb, only=None: [])
    assert zcgs.sync() == 0


def test_sweep_wrapper_delegates(monkeypatch):
    from interpreter import sweeps
    monkeypatch.setattr("ingestion.zendesk_case_graph_sync.sync", lambda **kw: 0)
    out = sweeps.zendesk_case_graph_sync(object(), dry_run=True)
    assert out == {"ok": True, "dry_run": True}
