"""Phase 27c — the case-control-plane field writes on each node.

`case_events.record` no-ops under pytest, so what's under test is the
Salesforce field write each node makes via `salesforce.update_case_fields`.
"""

from __future__ import annotations

import pytest
from dotenv import load_dotenv

load_dotenv()

from interpreter import case_events, salesforce
from interpreter.registry import (
    _cp_fields, h_ask_human, h_clarify, h_confidence_gate, h_handover, h_sf_writeback,
)


@pytest.fixture
def capture(monkeypatch):
    """Record every Case field write; return a live list of (sf_id, fields)."""
    calls: list[tuple] = []

    def fake(sf_id, fields, *, append=None, tenant_id=None):
        calls.append((sf_id, dict(fields)))
        return {"dry_run": True, "written": {}, "skipped": {}, "planned": fields, "target": sf_id}

    monkeypatch.setattr(salesforce, "update_case_fields", fake)
    return calls


def _fields_for(calls, key):
    """Merge every write that carried `key` — nodes may write more than once."""
    out = {}
    for _sf, f in calls:
        if key in f:
            out.update(f)
    return out


# ── helpers ──────────────────────────────────────────────────────────────
def test_case_events_record_is_a_noop_under_pytest():
    assert case_events.record(None, tenant_id="t", case_sf_id="500X",
                              actor="ai", action="route") is None


def test_cp_fields_builds_only_present_keys():
    f = _cp_fields(status="Escalated", routed_team="tier2", due_minutes=30,
                   escalation_reason="low_confidence", confidence=0.4123)
    assert f["Status"] == "Escalated"
    assert f["Routed_Team__c"] == "tier2"
    assert f["AI_Confidence__c"] == 0.41            # rounded to 2dp
    assert "Next_Action_Due__c" in f and f["Next_Action_Due__c"].endswith("+00:00")
    assert f["Escalation_Reason__c"] == "low_confidence"
    assert "Handoff_Slack_Ts__c" not in f


# ── sf_writeback ─────────────────────────────────────────────────────────
def test_sf_writeback_sets_triaged_and_routed_team(capture):
    state = {"case": {"sf_id": "500A"}, "routed_team": "csm",
             "classification": {"topic": "billing", "case_type": "Billing"}}
    h_sf_writeback(state, {"_node_id": "w"})
    f = _fields_for(capture, "Status")
    assert f["Status"] == "Triaged"
    assert f["Routed_Team__c"] == "csm"
    assert "Last_AI_Run_At__c" in f


def test_sf_writeback_does_not_downgrade_an_escalated_case(capture):
    state = {"case": {"sf_id": "500A", "status": "Escalated"}, "routed_team": "support",
             "classification": {"topic": "billing"}}
    h_sf_writeback(state, {"_node_id": "w"})
    for _sf, f in capture:
        assert f.get("Status") != "Triaged"


def test_sf_writeback_advance_status_can_be_disabled(capture):
    state = {"case": {"sf_id": "500A"}, "classification": {"topic": "x"}}
    h_sf_writeback(state, {"_node_id": "w", "advance_status": False})
    for _sf, f in capture:
        assert "Status" not in f


# ── confidence_gate ──────────────────────────────────────────────────────
def test_confidence_gate_writes_ai_confidence(capture):
    state = {"case": {"sf_id": "500A"}, "tier": "basic",
             "retrieval_score": 0.5, "draft_confidence": 0.5,
             "groundedness": {"score": 0.5}}
    h_confidence_gate(state, {"_node_id": "g"})
    f = _fields_for(capture, "AI_Confidence__c")
    assert isinstance(f["AI_Confidence__c"], float)
    assert "Status" not in f          # the gate never changes Status


# ── ask_human / handover ─────────────────────────────────────────────────
def test_ask_human_escalates_with_team_and_ack_clock(capture, monkeypatch):
    monkeypatch.setattr(salesforce, "assign_case", lambda *a, **k: {"assigned": False})
    state = {"case": {"sf_id": "500A"}, "routed_team": "csm", "confidence": 0.3,
             "confidence_gate": {"forced_escalation": "answer_mode 'action'"}}
    h_ask_human(state, {"_node_id": "ah", "channel": "salesforce_chatter",
                        "post_note": False})
    f = _fields_for(capture, "Status")
    assert f["Status"] == "Escalated"
    assert f["Routed_Team__c"] == "csm"
    assert f["Escalation_Reason__c"] == "answer_mode 'action'"
    assert "Next_Action_Due__c" in f


def test_handover_escalates(capture, monkeypatch):
    monkeypatch.setattr(salesforce, "assign_case", lambda *a, **k: {"assigned": True, "owner_type": "queue"})
    state = {"case": {"sf_id": "500A"}, "routed_team": "offboarding", "tier": "basic",
             "confidence": 0.2}
    h_handover(state, {"_node_id": "ho", "reason": "enterprise_or_offboarding",
                       "queue_by_team": {"offboarding": "Team_Offboarding"}})
    f = _fields_for(capture, "Status")
    assert f["Status"] == "Escalated"
    assert f["Routed_Team__c"] == "offboarding"


# ── clarify ──────────────────────────────────────────────────────────────
def test_clarify_sets_waiting_on_customer(capture, monkeypatch):
    monkeypatch.setattr(salesforce, "post_chatter", lambda *a, **k: {"posted": True, "dry_run": True})
    monkeypatch.setattr("interpreter.registry.llm.complete",
                        lambda *a, **k: '{"questions": ["What error do you see?"], "missing": ["error text"]}')
    state = {"case": {"sf_id": "500A", "contact": {"email": "c@x.com"}, "body": "it broke"},
             "confidence": 0.1}
    h_clarify(state, {"_node_id": "c", "max_rounds": 2, "_sb": None})
    f = _fields_for(capture, "Status")
    assert f["Status"] == "Waiting on Customer"
    assert "Next_Action_Due__c" in f
