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
    monkeypatch.setattr(sweeps, "_sf_targets", lambda _sb: [(None, sf)])

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


def test_reasoning_ttl_autonomous_continue_resolves(monkeypatch):
    """Phase 29 step 5 — a stalled `clarifying` session the bot can close out
    itself off documentation moves to `awaiting_approval` with a draft
    instead of being escalated + abandoned."""
    from interpreter import reasoning

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(salesforce, "available", lambda *a, **k: False)
    monkeypatch.setattr(reasoning, "_case_for_session", lambda _sb, _s: {"subject": "x"})
    monkeypatch.setattr(reasoning, "autonomous_continue",
                        lambda _s, _case, **_k: {"pointers": [{"q": "Q", "critical": True,
                                                                "answered": True, "agent_note": "n"}],
                                                 "resolved": True, "iterations": 1,
                                                 "kb_hits": ["doc says X"]})
    monkeypatch.setattr(reasoning, "_compose_draft", lambda *a, **k: "Here's our answer.")
    rows = [
        {"session_id": "s2", "state": "clarifying", "case_id": "500H", "case_number": "0008",
         "slack_channel": "#x", "slack_thread_ts": "2.2", "tenant_id": "t",
         "pointers": [{"q": "Q", "critical": True, "answered": False, "agent_note": None}],
         "updated_at": (now - timedelta(minutes=600)).isoformat()},
    ]
    sb = _SessSB(rows)
    out = sweeps.reasoning_ttl(sb, dry_run=False)
    assert out["continued"] == ["0008"] and out["escalated"] == []
    assert {"state": "awaiting_approval", "pointers": rows[0]["pointers"],
            "draft": "Here's our answer.", "updated_at": "now()"} in sb.updated


def test_reasoning_ttl_autonomous_continue_falls_back_to_escalate(monkeypatch):
    """When the bot can't close the gaps itself, the existing escalate +
    abandon path runs unchanged."""
    from interpreter import reasoning

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(salesforce, "available", lambda *a, **k: False)
    monkeypatch.setattr(reasoning, "_case_for_session", lambda _sb, _s: {"subject": "x"})
    monkeypatch.setattr(reasoning, "autonomous_continue",
                        lambda _s, _case, **_k: {"pointers": [], "resolved": False,
                                                 "iterations": 1, "kb_hits": []})
    rows = [
        {"session_id": "s2", "state": "clarifying", "case_id": "500H", "case_number": "0008",
         "slack_channel": "#x", "slack_thread_ts": "2.2", "tenant_id": "t",
         "pointers": [{"q": "Q", "critical": True, "answered": False, "agent_note": None}],
         "updated_at": (now - timedelta(minutes=600)).isoformat()},
    ]
    sb = _SessSB(rows)
    out = sweeps.reasoning_ttl(sb, dry_run=False)
    assert out["continued"] == [] and out["escalated"] == ["0008"]
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


# ── failed_jobs_sweep (robustness pass, 2026-09-03) ──────────────────────
class _JobsSB:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        return self

    def select(self, *a):
        return self

    def eq(self, *a):
        return self

    def gte(self, *a):
        return self

    def order(self, *a):
        return self

    def limit(self, *a):
        return self

    def execute(self):
        return type("R", (), {"data": self.rows})()


def test_failed_jobs_sweep_pages_once_per_failed_job(monkeypatch):
    paged = []
    monkeypatch.setattr(sweeps, "_page", lambda text, **k: paged.append(text))
    sb = _JobsSB([
        {"job_id": "j1", "kind": "run_flow", "error": "REQUEST_LIMIT_EXCEEDED",
         "attempts": 3, "updated_at": "2026-09-03T00:00:00Z"},
        {"job_id": "j2", "kind": "embed_kb_entry", "error": "timeout",
         "attempts": 3, "updated_at": "2026-09-03T00:01:00Z"},
    ])
    out = sweeps.failed_jobs_sweep(sb, dry_run=False)
    assert out["failed"] == 2
    assert len(paged) == 2
    assert "run_flow" in paged[0] and "j1" in paged[0]


def test_failed_jobs_sweep_dry_run_pages_nothing(monkeypatch):
    paged = []
    monkeypatch.setattr(sweeps, "_page", lambda text, **k: paged.append(text))
    sb = _JobsSB([{"job_id": "j1", "kind": "run_flow", "error": "boom",
                  "attempts": 3, "updated_at": "2026-09-03T00:00:00Z"}])
    out = sweeps.failed_jobs_sweep(sb, dry_run=True)
    assert out["dry_run"] is True
    assert paged == []


def test_failed_jobs_sweep_query_failure_is_a_clean_skip(monkeypatch):
    class _Broken(_JobsSB):
        def execute(self):
            raise RuntimeError("db down")

    out = sweeps.failed_jobs_sweep(_Broken([]), dry_run=False)
    assert "error" in out


# ── billing_trial_sweep (Billing & Payments chunk D, 2026-09-10) ────────
# billing_trial_sweep runs several distinct queries against the *same*
# tenants/audit_log tables in one call (reminder / grace-start /
# grace-nudge / lock), so — unlike the fakes above, which just replay one
# fixed row list regardless of the query — this one actually applies
# .eq/.lt/.gt filters, closely enough to tell those four queries apart.
class _BillingTable:
    def __init__(self, sb, name):
        self.sb, self.name = sb, name
        self.filters = []
        self._update = None

    def select(self, *_a):
        return self

    def eq(self, field, value):
        self.filters.append(("eq", field, value))
        return self

    def lt(self, field, value):
        self.filters.append(("lt", field, value))
        return self

    def gt(self, field, value):
        self.filters.append(("gt", field, value))
        return self

    def gte(self, field, value):
        self.filters.append(("gte", field, value))
        return self

    def limit(self, *_a):
        return self

    def order(self, *_a):
        return self

    def update(self, fields):
        self._update = fields
        return self

    def insert(self, row):
        self.sb.rows.setdefault(self.name, []).append(dict(row))
        return self

    def _matches(self, row):
        for op, field, value in self.filters:
            v = row.get(field)
            if op == "eq" and v != value:
                return False
            if op == "lt" and not (v is not None and v < value):
                return False
            if (op == "gt" and not (v is not None and v > value)) or \
               (op == "gte" and not (v is not None and v >= value)):
                return False
        return True

    def execute(self):
        if self._update is not None:
            for row in self.sb.rows.get(self.name, []):
                if self._matches(row):
                    row.update(self._update)
            return type("R", (), {"data": None})()
        return type("R", (), {"data": [r for r in self.sb.rows.get(self.name, []) if self._matches(r)]})()


class _BillingSB:
    def __init__(self, tenants=None, audit_log=None):
        self.rows = {"tenants": tenants or [], "audit_log": audit_log or [],
                     "tenant_integrations": []}

    def table(self, name):
        return _BillingTable(self, name)


def test_billing_sweep_reminds_a_trial_ending_soon_once():
    now = datetime.now(timezone.utc)
    sb = _BillingSB(tenants=[
        {"tenant_id": "t1", "name": "Acme", "billing_status": "trialing",
         "trial_ends_at": (now + timedelta(days=1)).isoformat()},
    ])
    out = sweeps.billing_trial_sweep(sb, dry_run=False)
    assert out["reminded"] == ["t1"]
    assert sb.rows["audit_log"][0]["action"] == "billing.trial_ending_soon"

    # a second sweep pass must not remind the same tenant again
    out2 = sweeps.billing_trial_sweep(sb, dry_run=False)
    assert out2["reminded"] == []


def test_billing_sweep_does_not_remind_a_trial_ending_far_out():
    now = datetime.now(timezone.utc)
    sb = _BillingSB(tenants=[
        {"tenant_id": "t1", "name": "Acme", "billing_status": "trialing",
         "trial_ends_at": (now + timedelta(days=6)).isoformat()},
    ])
    out = sweeps.billing_trial_sweep(sb, dry_run=False)
    assert out["reminded"] == []


def test_billing_sweep_moves_a_lapsed_trial_to_grace():
    now = datetime.now(timezone.utc)
    tenant = {"tenant_id": "t1", "name": "Acme", "billing_status": "trialing",
             "trial_ends_at": (now - timedelta(hours=1)).isoformat()}
    sb = _BillingSB(tenants=[tenant])
    out = sweeps.billing_trial_sweep(sb, dry_run=False)
    assert out["started_grace"] == ["t1"]
    assert tenant["billing_status"] == "grace"
    assert tenant["grace_ends_at"] > now.isoformat()   # trial_ends_at + GRACE_DAYS, still in the future
    assert sb.rows["audit_log"][0]["action"] == "billing.trial_ended_grace_started"


def test_billing_sweep_dry_run_changes_nothing():
    now = datetime.now(timezone.utc)
    tenant = {"tenant_id": "t1", "name": "Acme", "billing_status": "trialing",
             "trial_ends_at": (now - timedelta(hours=1)).isoformat()}
    sb = _BillingSB(tenants=[tenant])
    out = sweeps.billing_trial_sweep(sb, dry_run=True)
    assert out["started_grace"] == ["t1"] and out["dry_run"] is True
    assert tenant["billing_status"] == "trialing"   # unchanged
    assert sb.rows["audit_log"] == []


def test_billing_sweep_nudges_an_open_grace_once_per_day():
    now = datetime.now(timezone.utc)
    sb = _BillingSB(tenants=[
        {"tenant_id": "t1", "name": "Acme", "billing_status": "grace",
         "grace_ends_at": (now + timedelta(days=1)).isoformat()},
    ])
    out = sweeps.billing_trial_sweep(sb, dry_run=False)
    assert out["warned_grace"] == ["t1"]
    out2 = sweeps.billing_trial_sweep(sb, dry_run=False)
    assert out2["warned_grace"] == []   # already nudged today


def test_billing_sweep_locks_an_expired_grace():
    now = datetime.now(timezone.utc)
    tenant = {"tenant_id": "t1", "name": "Acme", "billing_status": "grace",
             "grace_ends_at": (now - timedelta(hours=1)).isoformat()}
    sb = _BillingSB(tenants=[tenant])
    out = sweeps.billing_trial_sweep(sb, dry_run=False)
    assert out["locked"] == ["t1"]
    assert tenant["billing_status"] == "locked"
    assert sb.rows["audit_log"][0]["action"] == "billing.locked"


def test_billing_sweep_active_tenant_is_untouched():
    sb = _BillingSB(tenants=[{"tenant_id": "t1", "name": "Acme", "billing_status": "active"}])
    out = sweeps.billing_trial_sweep(sb, dry_run=False)
    assert out == {"reminded": [], "started_grace": [], "warned_grace": [], "locked": [], "dry_run": False}


def test_billing_sweep_query_failure_is_a_clean_skip():
    class _Broken(_BillingSB):
        def table(self, name):
            class _T(_BillingTable):
                def execute(self_):
                    raise RuntimeError("db down")
            return _T(self, name)

    out = sweeps.billing_trial_sweep(_Broken(), dry_run=False)
    assert out == {"reminded": [], "started_grace": [], "warned_grace": [], "locked": [], "dry_run": False}


# ── interpreter/billing.py::assert_not_locked (the enforcement gate) ───
def test_assert_not_locked_raises_for_a_locked_tenant():
    from interpreter import billing

    sb = _BillingSB(tenants=[{"tenant_id": "t1", "billing_status": "locked"}])
    with pytest.raises(billing.BillingLockedError):
        billing.assert_not_locked("t1", sb)


def test_assert_not_locked_allows_every_other_status():
    from interpreter import billing

    for status in ("trialing", "active", "grace", "canceled"):
        sb = _BillingSB(tenants=[{"tenant_id": "t1", "billing_status": status}])
        billing.assert_not_locked("t1", sb)   # must not raise


def test_assert_not_locked_lets_a_missing_tenant_id_through():
    from interpreter import billing

    billing.assert_not_locked(None, _BillingSB())   # must not raise
