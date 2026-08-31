"""
Phase 20n — Case.Type triage + the `notify` node (ping an internal rep on the
Case without reassigning it) + `clarify` handover-on-exhaustion.

Offline: no DB / network / Salesforce. The classifier LLM is not exercised
here — `h_classify`'s Type derivation is covered through the deterministic
`salesforce.map_case_type` fallback.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import routing, salesforce
from interpreter.builder import build_graph
from interpreter.flows.validate_flow import Flow, check_flow
from interpreter.registry import h_clarify, h_confidence_gate, h_notify, h_sf_writeback

_HERMETIC = ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN", "SF_CONSUMER_KEY",
             "SF_CONSUMER_SECRET", "SF_PRIVATE_KEY", "SF_PRIVATE_KEY_FILE",
             "GROQ_API_KEY", "ANTHROPIC_API_KEY")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    for k in _HERMETIC:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(salesforce, "_client_obj", None, raising=False)
    routing._cache.clear()   # the resolve_notify_target TTL cache is module-level
    # no notify_targets rows unless a test provides them (keeps h_notify off the DB)
    monkeypatch.setattr(routing, "_fetch_rows", lambda tenant_id, sb: [])


for _k in _HERMETIC:
    os.environ.pop(_k, None)


# --------------------------------------------------------------------------
# Case.Type helpers
# --------------------------------------------------------------------------
def test_normalize_case_type_coerces_to_the_picklist():
    assert salesforce.normalize_case_type("problem / bug") == "Problem / Bug"
    assert salesforce.normalize_case_type("BILLING") == "Billing"
    assert salesforce.normalize_case_type("account_login") == "Account / Login"
    assert salesforce.normalize_case_type("feature-request") == "Feature Request"
    assert salesforce.normalize_case_type("bug") == "Problem / Bug"
    assert salesforce.normalize_case_type("something off-list") == ""
    assert salesforce.normalize_case_type(None) == ""


def test_map_case_type_keyword_fallback():
    assert salesforce.map_case_type("refund-request") == "Billing"
    assert salesforce.map_case_type("sso login broken") == "Account / Login"
    assert salesforce.map_case_type("api 500 error on webhook") == "Problem / Bug"
    assert salesforce.map_case_type("how do i export my data") == "How-to"
    assert salesforce.map_case_type("general question about zaps") == "Question"
    assert salesforce.map_case_type("") == ""


# --------------------------------------------------------------------------
# sf_writeback now writes Case.Type on every pass
# --------------------------------------------------------------------------
def test_sf_writeback_plans_a_type_write():
    state = {
        "case": {},  # no sf_id -> dry-run, `planned` holds the intended fields
        "classification": {"topic": "double-charge", "case_type": "Billing", "urgency": "high"},
    }
    out = h_sf_writeback(state, {"_node_id": "w"})
    planned = out["sf_writeback"]["planned"]
    assert planned["Type"] == "Billing"
    assert planned["Priority"] == "High"


def test_sf_writeback_derives_type_when_classifier_omitted_it():
    state = {"case": {}, "classification": {"topic": "password reset help", "urgency": "normal"}}
    out = h_sf_writeback(state, {"_node_id": "w"})
    assert out["sf_writeback"]["planned"]["Type"] == "Account / Login"


# --------------------------------------------------------------------------
# confidence_gate — escalate_types
# --------------------------------------------------------------------------
def test_escalate_types_forces_escalation():
    cfg = {"_node_id": "g", "default_threshold": 0.0,  # would PASS on score alone
           "weights": {"retrieval": 1, "draft": 0, "groundedness": 0},
           "escalate_types": ["Billing", "Account / Login"]}
    state = {"retrieval_score": 1.0, "tier": "basic",
             "classification": {"topic": "help", "case_type": "Billing"}}
    gate = h_confidence_gate(state, cfg)["confidence_gate"]
    assert gate["pass"] is False
    assert gate["forced_escalation"] == "type 'Billing'"

    # a non-listed type still passes
    state["classification"]["case_type"] = "How-to"
    assert h_confidence_gate(state, cfg)["confidence_gate"]["pass"] is True


# --------------------------------------------------------------------------
# notify — ping the rep, never touch OwnerId
# --------------------------------------------------------------------------
def test_notify_routes_by_type_then_module_then_fallback(monkeypatch):
    posted = []
    monkeypatch.setattr(salesforce, "post_chatter",
                        lambda cid, body, **k: (posted.append((cid, body, k)) or {"posted": True, "dry_run": False}))
    monkeypatch.setattr(salesforce, "add_case_comment",
                        lambda *a, **k: {"created": True})
    # assign_case must NOT be called by notify
    monkeypatch.setattr(salesforce, "assign_case",
                        lambda *a, **k: pytest.fail("notify must not reassign the Case"))

    cfg = {"_node_id": "n",
           "target_by_type": {"Billing": "00G000000000ABCAA0"},
           "target_by_module": {"API & Webhooks": "someone-in-api"},
           "fallback_target": "fallback-person"}

    # 1) type wins — and a 15/18-char id is passed through as an @mention
    out = h_notify({"case": {"sf_id": "500X"}, "draft": "hi",
                    "classification": {"topic": "refund", "case_type": "Billing"}}, cfg)
    assert out["outcome"]["target"] == "00G000000000ABCAA0"
    assert out["outcome"]["label"] == "Billing"
    assert out["outcome"]["reassigned"] is False
    assert posted[-1][2]["mention_id"] == "00G000000000ABCAA0"

    # 2) no type target -> module target (a plain name -> not an @mention)
    out = h_notify({"case": {"sf_id": "500X"},
                    "classification": {"topic": "webhook", "case_type": "Problem / Bug"},
                    "sf_writeback": {"written": {"Module__c": "API & Webhooks"}}}, cfg)
    assert out["outcome"]["target"] == "someone-in-api"
    assert posted[-1][2]["mention_id"] is None

    # 3) neither -> fallback_target
    out = h_notify({"case": {"sf_id": "500X"},
                    "classification": {"topic": "mystery", "case_type": "Other"}}, cfg)
    assert out["outcome"]["target"] == "fallback-person"


def test_notify_without_sf_id_is_a_safe_noop():
    out = h_notify({"case": {}, "classification": {"case_type": "Billing"}},
                   {"_node_id": "n", "target_by_type": {}})
    assert out["outcome"]["action"] == "notify"
    assert out["outcome"]["reassigned"] is False
    assert "not posted" in out["trace"][0]["summary"]


# --------------------------------------------------------------------------
# clarify — hand to the support queue only once the round cap is hit
# --------------------------------------------------------------------------
def test_clarify_handover_queue_only_fires_when_exhausted(monkeypatch):
    assigned = []
    monkeypatch.setattr(salesforce, "assign_case",
                        lambda cid, **k: (assigned.append((cid, k)) or {"assigned": True}))
    monkeypatch.setattr(salesforce, "post_chatter", lambda *a, **k: {"posted": True, "dry_run": True})
    monkeypatch.setattr(salesforce, "send_case_reply", lambda *a, **k: {"sent": False})

    class _FakeSB:
        def table(self, *_): return self
        def select(self, *_): return self
        def eq(self, *_): return self
        def execute(self): return type("R", (), {"data": []})()

    base = {"case": {"sf_id": "500Y", "case_id": "cid-1"}, "tenant_id": None}
    cfg = {"_node_id": "cl", "max_rounds": 2, "handover_queue": "Team_Support", "_sb": _FakeSB()}

    # round 1 — not exhausted, no handover
    h_clarify(dict(base), cfg)
    assert assigned == []

    # round 3 — exhausted, hands to Team_Support
    class _SB3(_FakeSB):
        def execute(self): return type("R", (), {"data": [{"clarify_round": 2}]})()
    cfg3 = {**cfg, "_sb": _SB3()}
    out = h_clarify(dict(base), cfg3)
    assert assigned and assigned[-1][1]["queue"] == "Team_Support"
    assert out["outcome"]["handover_queue"] == "Team_Support"


# --------------------------------------------------------------------------
# notify_targets — the central routing table (Phase 20o)
# --------------------------------------------------------------------------
_ROWS = [
    {"match_kind": "case_type", "match_value": "Billing", "resolver": "sf_queue",
     "sf_queue": "Billing_Escalations", "label": "Billing team", "active": True},
    {"match_kind": "case_type", "match_value": "Problem / Bug", "resolver": "sf_team_role",
     "sf_team": "Support", "sf_role": "Manager", "label": None, "active": True},
    {"match_kind": "case_type", "match_value": "Feature Request", "resolver": "static",
     "sf_target_id": "005000000000ABCAA0", "sf_target_type": "user", "label": "Product", "active": True},
    {"match_kind": "module", "match_value": "API & Webhooks", "resolver": "static",
     "sf_target_id": "someone", "sf_target_type": "user", "label": "API team", "active": True},
]


def test_resolve_notify_target_static_queue_and_team(monkeypatch):
    monkeypatch.setattr(routing, "_fetch_rows", lambda t, sb: _ROWS)
    monkeypatch.setattr(salesforce, "available", lambda *a, **k: True)

    class _SF:
        def query(self, soql):
            if "FROM User" in soql:
                return {"records": [{"Id": "005000000000MGRAA0", "Name": "Sam Rivera"}]}
            if "Type = 'Queue'" in soql:
                return {"records": [{"Id": "00G000000000QQQAA0"}]}
            return {"records": []}
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _SF())

    # static
    r = routing.resolve_notify_target("t", "Feature Request", None)
    assert r["id"] == "005000000000ABCAA0" and r["type"] == "user" and r["label"] == "Product"

    # sf_queue -> resolves the Group id, type=queue
    r = routing.resolve_notify_target("t", "Billing", None)
    assert r["id"] == "00G000000000QQQAA0" and r["type"] == "queue" and r["label"] == "Billing team"

    # sf_team_role -> the live team member; label built from the name
    r = routing.resolve_notify_target("t", "Problem / Bug", None)
    assert r["id"] == "005000000000MGRAA0" and r["type"] == "user"
    assert "Sam Rivera" in r["label"] and "Support" in r["label"]

    # Case.Type miss -> falls through to a Module row
    r = routing.resolve_notify_target("t", "Question", "API & Webhooks")
    assert r["id"] == "someone" and r["label"] == "API team"

    # nothing matches
    assert routing.resolve_notify_target("t", "Question", "Zaps") is None


def test_resolve_degrades_without_sf_creds(monkeypatch):
    monkeypatch.setattr(routing, "_fetch_rows", lambda t, sb: _ROWS)
    monkeypatch.setattr(salesforce, "available", lambda *a, **k: False)
    r = routing.resolve_notify_target("t", "Billing", None)  # sf_queue, no creds
    assert r["id"] is None and r["type"] is None and r["label"] == "Billing team"


def test_h_notify_falls_back_to_the_table(monkeypatch):
    posted = []
    monkeypatch.setattr(salesforce, "post_chatter",
                        lambda cid, body, **k: (posted.append(k) or {"posted": True, "dry_run": False}))
    monkeypatch.setattr(salesforce, "add_case_comment", lambda *a, **k: {"created": True})
    monkeypatch.setattr(salesforce, "assign_case",
                        lambda *a, **k: pytest.fail("notify must not reassign"))
    monkeypatch.setattr(routing, "resolve_notify_target",
                        lambda tid, ct, mod, **k: {"id": "005000000000USRAA0", "type": "user",
                                                   "label": "Billing team", "resolver": "sf_queue"})

    # node config carries NO target maps -> the table is consulted
    out = h_notify({"case": {"sf_id": "500X"}, "draft": "hi", "tenant_id": "t",
                    "classification": {"topic": "refund", "case_type": "Billing"}},
                   {"_node_id": "n"})
    assert out["outcome"]["target"] == "005000000000USRAA0"
    assert out["outcome"]["resolved_via"] == "table:sf_queue"
    assert out["outcome"]["label"] == "Billing team"
    assert posted[-1]["mention_id"] == "005000000000USRAA0"   # user id -> @mention

    # a node override still wins over the table
    monkeypatch.setattr(routing, "resolve_notify_target",
                        lambda *a, **k: pytest.fail("override should short-circuit the table"))
    out = h_notify({"case": {"sf_id": "500X"}, "tenant_id": "t",
                    "classification": {"case_type": "Billing", "topic": "x"}},
                   {"_node_id": "n", "target_by_type": {"Billing": "OVERRIDE"}})
    assert out["outcome"]["target"] == "OVERRIDE"
    assert out["outcome"]["resolved_via"] == "node_config"


# --------------------------------------------------------------------------
# both redesigned flows still compile and route
# --------------------------------------------------------------------------
@pytest.mark.parametrize("fname", ["flow_email_l0l1.json", "flow_case_router.json"])
def test_redesigned_flows_compile(fname):
    p = pathlib.Path(__file__).resolve().parents[1] / "interpreter/flows" / fname
    flow = json.loads(p.read_text())
    assert check_flow(Flow.model_validate(flow), require_expected_types=False) == []
    build_graph(flow)
    types = {n["type"] for n in flow["nodes"]}
    assert {"notify", "clarify"} <= types
