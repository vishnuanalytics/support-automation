"""
Offline unit tests for the Phase 20e `sf_case` node — no DB, no network, no
Salesforce. With no SF creds `salesforce.ensure_case` is a dry-run, so the
node runs but creates nothing; the merge-into-`state.case` logic and the
seeded email L0/L1 flow's wiring are what's checked here.

Run:  pytest tests/test_sf_case.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import salesforce
from interpreter.builder import build_graph
from interpreter.flows.validate_flow import Flow, check_flow
from interpreter.registry import h_ask_human, h_handover, h_sf_case, h_sf_writeback

_HERMETIC = ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN", "SF_CONSUMER_KEY",
             "SF_CONSUMER_SECRET", "SF_PRIVATE_KEY", "SF_PRIVATE_KEY_FILE")


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    for k in _HERMETIC:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(salesforce, "_client_obj", None, raising=False)
    assert not salesforce.available()


for _k in _HERMETIC:
    os.environ.pop(_k, None)
salesforce._client_obj = None

_CFG = {"_node_id": "sf_case", "origin": "Email", "status": "New"}
_EMAIL_CASE = {
    "channel": "email",
    "from": "newperson@example.com",
    "from_name": "New Person",
    "subject": "Cannot connect my Zap",
    "body": "The trigger step errors with 'auth failed'.",
    "message_id": "<abc@mail>",
}


# --------------------------------------------------------------------------
# salesforce.ensure_case — dry-run
# --------------------------------------------------------------------------
def test_ensure_case_dry_run_creates_nothing():
    info = salesforce.ensure_case(dict(_EMAIL_CASE))
    assert info["dry_run"] is True
    assert info["sf_id"] is None
    assert info["created"] is False and info["contact_created"] is False
    assert info["reason"] == "salesforce not configured"


def test_ensure_case_keeps_an_existing_sf_id():
    info = salesforce.ensure_case({**_EMAIL_CASE, "sf_id": "500XXXXXXXXXXXX"})
    assert info["sf_id"] == "500XXXXXXXXXXXX"


# --------------------------------------------------------------------------
# h_sf_case — the node
# --------------------------------------------------------------------------
def test_h_sf_case_dry_run_is_passthrough_with_a_trace():
    out = h_sf_case({"case": dict(_EMAIL_CASE)}, dict(_CFG))
    assert out["trace"][0]["type"] == "sf_case"
    assert "dry-run" in out["trace"][0]["summary"]
    assert out["sf_case"]["dry_run"] is True
    # nothing invented onto the case
    assert "sf_id" not in out["case"]


def test_h_sf_case_merges_sf_id_and_account_snapshot(monkeypatch):
    def fake_ensure_case(case, sender=None, **kw):
        return {
            "sf_id": "500AAA", "case_number": "00001234",
            "contact_id": "003AAA", "account_id": "001AAA",
            "account_name": "Example Inc",
            "account": {"name": "Example Inc", "customer_type": "premium",
                        "region": "US"},
            "created": True, "reused": False,
            "contact_created": True, "account_created": False,
            "dry_run": False,
        }

    monkeypatch.setattr(salesforce, "ensure_case", fake_ensure_case)
    out = h_sf_case({"case": dict(_EMAIL_CASE)}, dict(_CFG))
    assert out["case"]["sf_id"] == "500AAA"
    # classify reads account.customer_type -> the real tier now flows through
    assert out["case"]["account"]["customer_type"] == "premium"
    assert out["case"]["account"]["region"] == "US"
    assert "created Case 500AAA" in out["trace"][0]["summary"]
    assert "new Contact" in out["trace"][0]["summary"]


def test_h_sf_case_reused_case_summary(monkeypatch):
    monkeypatch.setattr(salesforce, "ensure_case", lambda *a, **k: {
        "sf_id": "500BBB", "case_number": "00009999", "account": {},
        "reused": True, "created": False, "contact_created": False,
        "account_created": False, "dry_run": False,
    })
    out = h_sf_case({"case": dict(_EMAIL_CASE)}, dict(_CFG))
    assert "reused open Case 00009999" in out["trace"][0]["summary"]
    assert out["case"]["sf_id"] == "500BBB"


# --------------------------------------------------------------------------
# the seeded email L0/L1 flow
# --------------------------------------------------------------------------
def test_email_l0l1_portable_flow_compiles_and_routes():
    p = pathlib.Path(__file__).resolve().parents[1] / "interpreter/flows/flow_email_l0l1.json"
    flow = json.loads(p.read_text())
    assert check_flow(Flow.model_validate(flow), require_expected_types=False) == []
    build_graph(flow)   # raises on a bad entry / unknown type / routing gap
    types = [n["type"] for n in flow["nodes"]]
    assert types[0] == "identify" and types[1] == "sf_case"
    assert {"sf_case", "sf_writeback", "auto_reply", "ask_human", "handover"} <= set(types)
    # the three confidence_gate branches are mutually exclusive + exhaustive
    gate_conds = sorted(
        e["condition"]["if"] for e in flow["edges"]
        if e["source_node_id"] == "confidence_gate"
    )
    assert gate_conds == [
        "confidence_gate.pass and tier != 'enterprise'",
        "not confidence_gate.pass and tier != 'enterprise'",
        "tier == 'enterprise'",
    ]


# --------------------------------------------------------------------------
# Phase 20f — the email conversation lives on the Case
# --------------------------------------------------------------------------
def test_thread_msg_ids_dedupes_and_offers_both_bracket_forms():
    ids = salesforce._thread_msg_ids({
        "in_reply_to": "<root@a>",
        "references": ["<root@a>", "<r2@b>", ""],
    })
    assert ids == ["<root@a>", "root@a", "<r2@b>", "r2@b"]


def test_find_case_by_thread_and_helpers_are_safe_without_creds():
    assert salesforce.find_case_by_thread(["<x@y>"]) == {}
    assert salesforce.find_case_by_thread([]) == {}
    em = salesforce.log_email_message("500X", incoming=True, subject="hi",
                                      body="t", message_id="<m@x>")
    assert em == {"created": False, "dry_run": True, "id": None}
    assert salesforce.assign_case("500X")["assigned"] is False           # no target
    d = salesforce.assign_case("500X", queue="Support_Handover")
    assert d["assigned"] is False and d["dry_run"] is True


def test_ensure_case_reuses_the_thread_case(monkeypatch):
    monkeypatch.setattr(salesforce, "available", lambda *a, **k: True)
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: object())
    monkeypatch.setattr(salesforce, "_account_snapshot", lambda sf, aid: {})
    monkeypatch.setattr(salesforce, "find_case_by_thread",
                        lambda ids, **k: {"sf_id": "500THREAD", "case_number": "00007"} if ids else {})
    case = {"channel": "email", "from": "p@known.example", "references": ["<root@known>"]}
    info = salesforce.ensure_case(case, {"contact_id": "003K", "account_id": "001K"}, reuse="thread")
    assert info["reused"] is True and info["sf_id"] == "500THREAD"
    assert info["created"] is False


def test_ensure_case_reuse_never_always_creates(monkeypatch):
    monkeypatch.setattr(salesforce, "available", lambda *a, **k: True)
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: object())
    monkeypatch.setattr(salesforce, "_account_snapshot", lambda sf, aid: {})
    monkeypatch.setattr(salesforce, "find_case_by_thread",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))

    class _SF:
        Case = type("C", (), {"create": staticmethod(lambda p: {"id": "500NEW"})})()
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _SF())
    info = salesforce.ensure_case({"channel": "email", "references": ["<x@y>"]},
                                  {"contact_id": "003K"}, reuse="never")
    assert info["created"] is True and info["sf_id"] == "500NEW"


def test_h_sf_case_records_the_inbound_email_on_the_case(monkeypatch):
    monkeypatch.setattr(salesforce, "ensure_case", lambda *a, **k: {
        "sf_id": "500EM", "case_number": "00010", "account": {},
        "created": True, "reused": False, "contact_created": False,
        "account_created": False, "dry_run": False,
    })
    seen = {}
    monkeypatch.setattr(salesforce, "log_email_message",
                        lambda cid, **kw: (seen.update(kw, case=cid) or
                                           {"created": True, "dry_run": False, "id": "02sIN"}))
    out = h_sf_case({"case": dict(_EMAIL_CASE)}, dict(_CFG))
    assert out["sf_case"]["inbound_email"]["id"] == "02sIN"
    assert seen["case"] == "500EM" and seen["incoming"] is True
    assert seen["message_id"] == "<abc@mail>"


def test_h_ask_human_leaves_the_draft_as_a_case_comment(monkeypatch):
    # Salesforce won't take an API-created outbound draft EmailMessage, so the
    # suggested reply lands as an internal CaseComment (plus the Chatter note).
    monkeypatch.setattr(salesforce, "post_chatter",
                        lambda cid, body, **k: {"posted": False, "dry_run": True})
    seen = {}
    monkeypatch.setattr(salesforce, "add_case_comment",
                        lambda cid, body, **kw: (seen.update(kw, case=cid, body=body) or
                                                 {"created": True, "dry_run": False, "id": "00aDR"}))
    state = {
        "case": {"channel": "email", "sf_id": "500Z", "from": "cust@x.com", "subject": "Help"},
        "draft": "Here is a suggested answer.",
    }
    out = h_ask_human(state, {"_node_id": "ah", "channel": "salesforce_chatter"})
    assert out["outcome"]["draft_comment"]["id"] == "00aDR"
    assert seen["case"] == "500Z" and seen["published"] is False
    assert "Here is a suggested answer." in seen["body"]
    assert "draft comment" in out["trace"][0]["summary"]


def test_h_handover_reassigns_when_a_queue_is_configured(monkeypatch):
    seen = {}
    monkeypatch.setattr(salesforce, "assign_case",
                        lambda cid, **k: (seen.update(k, case=cid) or
                                          {"assigned": True, "owner_type": "queue", "owner_id": "00GX"}))
    out = h_handover({"case": {"sf_id": "500H"}},
                     {"_node_id": "ho", "reason": "enterprise_tier", "queue": "Support_Handover"})
    assert seen["case"] == "500H" and seen["queue"] == "Support_Handover"
    assert out["outcome"]["assignment"]["assigned"] is True
    assert "reassigned" in out["trace"][0]["summary"]


def test_h_handover_without_a_queue_is_unchanged():
    out = h_handover({"case": {"sf_id": "500H"}}, {"_node_id": "ho", "reason": "policy"})
    assert "assignment" not in out["outcome"]
    assert out["trace"][0]["summary"] == "full handover (policy)"


# --------------------------------------------------------------------------
# Phase 20g — classifier slug -> the Case picklists + queue routing
# --------------------------------------------------------------------------
def test_map_case_fields_slug_and_country():
    m = salesforce.map_case_fields("refund-for-duplicate-charge", "United Kingdom")
    assert m["Topic__c"] == "refund-for-duplicate-charge"
    assert m["Module__c"] == "Billing & Plans" and m["SubModule__c"] == "Refunds"
    assert m["Region__c"] == "EMEA"

    m = salesforce.map_case_fields("sso-login-broken", "India")
    assert m["Module__c"] == "Account & Login" and m["SubModule__c"] == "SSO"
    assert m["Region__c"] == "APAC"

    # unknown slug -> Other, no submodule; unknown country -> no region
    m = salesforce.map_case_fields("something-weird", "Narnia")
    assert m == {"Topic__c": "something-weird", "Module__c": "Other"}
    assert salesforce.map_case_fields("", None) == {}


def test_h_sf_writeback_writes_the_derived_picklists(monkeypatch):
    seen = {}
    monkeypatch.setattr(salesforce, "update_case_fields",
                        lambda cid, fields, **k: (seen.update(case=cid, fields=fields) or
                                                  {"written": fields, "skipped": {}, "dry_run": False,
                                                   "planned": {}}))
    state = {
        "case": {"sf_id": "500W", "account": {"region": "United States"}},
        "classification": {"topic": "webhook-not-firing", "urgency": "high"},
        "tier": "premium",
    }
    h_sf_writeback(state, {"_node_id": "w"})
    f = seen["fields"]
    assert f["Topic__c"] == "webhook-not-firing"
    assert f["Module__c"] == "API & Webhooks" and f["SubModule__c"] == "Webhooks"
    assert f["Region__c"] == "NA" and f["Priority"] == "High"  # value_map


def test_h_ask_human_routes_to_the_escalation_queue_on_forced_topic(monkeypatch):
    monkeypatch.setattr(salesforce, "post_chatter", lambda *a, **k: {"posted": False, "dry_run": True})
    seen = {}
    monkeypatch.setattr(salesforce, "assign_case",
                        lambda cid, **k: (seen.update(k, case=cid) or {"assigned": True, "owner_type": "queue"}))
    cfg = {"_node_id": "ah", "channel": "salesforce_chatter",
           "queue": "Support_L0L1", "escalate_queue": "Billing_Escalations"}

    # forced escalation -> escalate_queue
    h_ask_human({"case": {"sf_id": "500A"}, "confidence_gate": {"forced_escalation": "topic 'refund'"}}, cfg)
    assert seen["queue"] == "Billing_Escalations"

    # plain low-confidence fail (no forced flag) -> the default queue
    seen.clear()
    h_ask_human({"case": {"sf_id": "500B"}, "confidence_gate": {"pass": False}}, cfg)
    assert seen["queue"] == "Support_L0L1"

    # a module-family forced escalation (h_confidence_gate sets this for a
    # billing/plans topic whatever the slug) -> escalate_queue (20h)
    seen.clear()
    h_ask_human({"case": {"sf_id": "500C"},
                 "confidence_gate": {"forced_escalation": "module 'Billing & Plans'"}}, cfg)
    assert seen["queue"] == "Billing_Escalations"


def test_confidence_gate_forces_a_human_on_a_billing_module_topic():
    from interpreter.registry import h_confidence_gate
    # slug has no "billing"/"refund" token, but maps to the Billing & Plans module
    out = h_confidence_gate(
        {"classification": {"topic": "duplicate-annual-charge"},
         "retrieval_score": 0.9, "draft_confidence": 0.9, "groundedness": {"score": 0.9},
         "tier": "basic"},
        {"_node_id": "g", "default_threshold": 0.3},
    )
    g = out["confidence_gate"]
    assert g["pass"] is False and "Billing & Plans" in g["forced_escalation"]

    # opt out with escalate_modules: []
    out2 = h_confidence_gate(
        {"classification": {"topic": "duplicate-annual-charge"},
         "retrieval_score": 0.9, "draft_confidence": 0.9, "groundedness": {"score": 0.9},
         "tier": "basic"},
        {"_node_id": "g", "default_threshold": 0.3, "escalate_modules": []},
    )
    assert out2["confidence_gate"]["pass"] is True
