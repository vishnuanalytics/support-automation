"""Phase 27d — the safety-net sweeps (queue_sweep / cdc_reconcile / reasoning_ttl).

Salesforce + Slack + Supabase are all faked; what's under test is the ladder
logic (nudge vs breach) and the query wiring.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv

load_dotenv()

from interpreter import salesforce, sweeps


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000+0000")


@pytest.fixture(autouse=True)
def _no_slack(monkeypatch):
    monkeypatch.setattr(sweeps, "_page", lambda *a, **k: None)
    monkeypatch.setattr(sweeps, "_event", lambda *a, **k: None)


class _SF:
    def __init__(self, cases):
        self.cases = cases
        self.updates: list = []
        self.assigns: list = []

    def query(self, soql):
        return {"records": list(self.cases)}


def _sf_patch(monkeypatch, sf):
    monkeypatch.setattr(salesforce, "available", lambda *a, **k: True)
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: sf)
    monkeypatch.setattr(salesforce, "update_case_fields",
                        lambda cid, fields, **k: sf.updates.append((cid, fields)) or {"dry_run": False})
    monkeypatch.setattr(salesforce, "assign_case",
                        lambda cid, **k: sf.assigns.append((cid, k)) or {"assigned": True})


# ── queue_sweep ─────────────────────────────────────────────────────────
def test_queue_sweep_nudges_a_freshly_overdue_case(monkeypatch):
    now = datetime.now(timezone.utc)
    sf = _SF([{
        "Id": "500A", "CaseNumber": "0001", "Status": "In Progress",
        "OwnerId": "00Gxxx", "Routed_Team__c": "tier2",
        "Next_Action_Due__c": _iso(now - timedelta(minutes=5)),   # overdue, but < ACK_MIN
        "SLA_Breach__c": False, "CreatedDate": _iso(now - timedelta(hours=1)),
        "LastModifiedDate": _iso(now - timedelta(minutes=5)),
    }])
    _sf_patch(monkeypatch, sf)
    out = sweeps.queue_sweep(sf, dry_run=False)
    assert out["nudged"] == ["0001"] and out["breached"] == []
    assert sf.assigns and sf.assigns[0][1]["queue"] == "Support_Tier2"


def test_queue_sweep_breaches_a_long_overdue_case(monkeypatch):
    now = datetime.now(timezone.utc)
    sf = _SF([{
        "Id": "500B", "CaseNumber": "0002", "Status": "Escalated",
        "OwnerId": "00Gxxx", "Routed_Team__c": "support",
        "Next_Action_Due__c": _iso(now - timedelta(minutes=90)),  # overdue > 2x ACK
        "SLA_Breach__c": False, "CreatedDate": _iso(now - timedelta(hours=3)),
        "LastModifiedDate": _iso(now - timedelta(minutes=90)),
    }])
    _sf_patch(monkeypatch, sf)
    out = sweeps.queue_sweep(sf, dry_run=False)
    assert out["breached"] == ["0002"] and out["nudged"] == []
    assert any(f.get("SLA_Breach__c") is True for _c, f in sf.updates)
    assert any(k["queue"] == "SLA_Breach" for _c, k in sf.assigns)


def test_queue_sweep_skips_already_breached_and_resolved(monkeypatch):
    now = datetime.now(timezone.utc)
    sf = _SF([{
        "Id": "500C", "CaseNumber": "0003", "Status": "Escalated", "OwnerId": "00Gx",
        "Routed_Team__c": "support", "Next_Action_Due__c": _iso(now - timedelta(hours=5)),
        "SLA_Breach__c": True, "CreatedDate": _iso(now), "LastModifiedDate": _iso(now),
    }])
    _sf_patch(monkeypatch, sf)
    out = sweeps.queue_sweep(sf, dry_run=False)
    assert out["nudged"] == [] and out["breached"] == []


def test_queue_sweep_dry_run_changes_nothing(monkeypatch):
    now = datetime.now(timezone.utc)
    sf = _SF([{
        "Id": "500D", "CaseNumber": "0004", "Status": "New", "OwnerId": "00Gx",
        "Routed_Team__c": "support", "Next_Action_Due__c": None,
        "SLA_Breach__c": False, "CreatedDate": _iso(now - timedelta(hours=2)),
        "LastModifiedDate": _iso(now - timedelta(hours=2)),
    }])
    _sf_patch(monkeypatch, sf)
    out = sweeps.queue_sweep(sf, dry_run=True)
    assert out["breached"] == ["0004"]        # stuck > 2x -> would breach
    assert sf.updates == [] and sf.assigns == []


def test_queue_sweep_stuck_check_uses_last_run_not_created_date(monkeypatch):
    """A Case re-triaged on a customer reply has an old CreatedDate but a
    fresh Last_AI_Run_At__c — must not read as stuck."""
    now = datetime.now(timezone.utc)
    sf = _SF([{
        "Id": "500Y", "CaseNumber": "0008", "Status": "Triaged", "OwnerId": "00Gx",
        "Routed_Team__c": "support", "Next_Action_Due__c": None,
        "Last_AI_Run_At__c": _iso(now - timedelta(minutes=1)),
        "SLA_Breach__c": False, "CreatedDate": _iso(now - timedelta(days=10)),
        "LastModifiedDate": _iso(now - timedelta(minutes=1)),
    }])
    _sf_patch(monkeypatch, sf)
    out = sweeps.queue_sweep(sf, dry_run=False)
    assert out["nudged"] == [] and out["breached"] == []


def test_queue_sweep_ignores_cases_the_pipeline_never_touched(monkeypatch):
    """A Case with none of the control-plane fields set predates the Phase
    27c cutover (or CDC hasn't run its first pass yet) — not the sweep's to
    judge. Regression: this used to breach an entire pre-existing backlog."""
    now = datetime.now(timezone.utc)
    sf = _SF([{
        "Id": "500Z", "CaseNumber": "0009", "Status": "New", "OwnerId": "00Gx",
        "Routed_Team__c": None, "Next_Action_Due__c": None, "Last_AI_Run_At__c": None,
        "SLA_Breach__c": False, "CreatedDate": _iso(now - timedelta(days=30)),
        "LastModifiedDate": _iso(now - timedelta(days=30)),
    }])
    _sf_patch(monkeypatch, sf)
    out = sweeps.queue_sweep(sf, dry_run=False)
    assert out["nudged"] == [] and out["breached"] == []
    assert sf.updates == [] and sf.assigns == []


def test_queue_sweep_no_creds_is_a_clean_skip(monkeypatch):
    monkeypatch.setattr(salesforce, "available", lambda *a, **k: False)
    assert sweeps.queue_sweep(object())["skipped"].startswith("no Salesforce")


# ── cdc_reconcile ───────────────────────────────────────────────────────
class _SB:
    def __init__(self, existing_run_ids):
        self._have = existing_run_ids
        self.enqueued: list = []

    def table(self, name):
        return self

    def select(self, *a):
        return self

    def or_(self, *a):
        return self

    def eq(self, *a):
        return self

    def limit(self, *a):
        return self

    def execute(self):
        return type("R", (), {"data": self._have})()


def test_cdc_reconcile_enqueues_only_cases_with_no_run(monkeypatch):
    now = datetime.now(timezone.utc)
    sf = _SF([{"Id": "500E", "CaseNumber": "0005"}, {"Id": "500F", "CaseNumber": "0006"}])
    monkeypatch.setattr(salesforce, "available", lambda *a, **k: True)
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: sf)

    calls: list = []
    monkeypatch.setattr("interpreter.sf_ingest.enqueue_case_run",
                        lambda sb, cid, **k: calls.append(cid))
    # first case has a run, second doesn't
    seq = iter([[{"run_id": "r1"}], []])
    sb = _SB([])
    monkeypatch.setattr(sb, "execute", lambda: type("R", (), {"data": next(seq)})())

    out = sweeps.cdc_reconcile(sb, dry_run=False)
    assert out["enqueued"] == ["0006"] and calls == ["500F"]


# ── reasoning_ttl ───────────────────────────────────────────────────────
class _SessSB:
    def __init__(self, rows):
        self.rows = rows
        self.updated: list = []

    def table(self, name):
        self._t = name
        return self

    def select(self, *a):
        return self

    def not_(self):
        return self

    # not_.in_ chain
    @property
    def not_(self):  # noqa: A003
        return self

    def in_(self, *a):
        return self

    def lt(self, *a):
        return self

    def limit(self, *a):
        return self

    def update(self, patch):
        self._patch = patch
        return self

    def eq(self, *a):
        self.updated.append(self._patch)
        return self

    def execute(self):
        return type("R", (), {"data": self.rows})()


def test_reasoning_ttl_nudges_then_escalates(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(salesforce, "available", lambda *a, **k: False)
    rows = [
        {"session_id": "s1", "state": "clarifying", "case_id": "500G", "case_number": "0007",
         "slack_channel": "#x", "slack_thread_ts": "1.1", "tenant_id": "t",
         "updated_at": (now - timedelta(minutes=150)).isoformat()},          # nudge
        {"session_id": "s2", "state": "clarifying", "case_id": "500H", "case_number": "0008",
         "slack_channel": "#x", "slack_thread_ts": "2.2", "tenant_id": "t",
         "updated_at": (now - timedelta(minutes=600)).isoformat()},          # escalate
    ]
    sb = _SessSB(rows)
    out = sweeps.reasoning_ttl(sb, dry_run=False)
    assert out["nudged"] == ["0007"] and out["escalated"] == ["0008"]
    assert {"state": "abandoned"} in sb.updated


def test_queue_sweep_auto_resolves_stale_waiting_on_customer(monkeypatch):
    now = datetime.now(timezone.utc)
    sf = _SF([{
        "Id": "500W", "CaseNumber": "0010", "Status": "Waiting on Customer",
        "OwnerId": "00Gx", "Routed_Team__c": "support",
        "Next_Action_Due__c": _iso(now - timedelta(hours=1)),
        "SLA_Breach__c": False, "CreatedDate": _iso(now - timedelta(days=5)),
        "LastModifiedDate": _iso(now - timedelta(days=3)),
        "Last_AI_Run_At__c": _iso(now - timedelta(days=3)),
    }])
    _sf_patch(monkeypatch, sf)
    monkeypatch.setattr(salesforce, "post_chatter", lambda *a, **k: {"posted": True})
    out = sweeps.queue_sweep(sf, dry_run=False)
    assert out["resolved"] == ["0010"] and out["breached"] == []
    assert any(f.get("Status") == "Resolved" for _c, f in sf.updates)


def test_queue_sweep_dead_letters_escalated_but_unrouted(monkeypatch):
    now = datetime.now(timezone.utc)
    sf = _SF([{
        "Id": "500U", "CaseNumber": "0011", "Status": "Escalated",
        "OwnerId": "00GAI_Intake000",          # still queue-owned by intake
        "Routed_Team__c": None,
        "Next_Action_Due__c": None, "Last_AI_Run_At__c": _iso(now - timedelta(hours=2)),
        "SLA_Breach__c": False, "CreatedDate": _iso(now - timedelta(hours=3)),
        "LastModifiedDate": _iso(now - timedelta(hours=2)),
    }])
    _sf_patch(monkeypatch, sf)
    out = sweeps.queue_sweep(sf, dry_run=False)
    assert out["breached"] == ["0011"]
    assert any(k["queue"] == "Unrouted_Review" for _c, k in sf.assigns)
