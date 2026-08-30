"""
Offline unit tests for the Phase 20i Case-router workflow: the `team_route`
node, the `queue_by_team` / `escalate_queue` resolution, and the seeded
flow's wiring. No DB / network / Salesforce.
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
from interpreter.registry import _route_queue, h_handover, h_team_route

_HERMETIC = ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN", "SF_CONSUMER_KEY",
             "SF_CONSUMER_SECRET", "SF_PRIVATE_KEY", "SF_PRIVATE_KEY_FILE")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    for k in _HERMETIC:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(salesforce, "_client_obj", None, raising=False)


for _k in _HERMETIC:
    os.environ.pop(_k, None)


def _route(subject, topic="unknown", body=""):
    st = {"case": {"subject": subject, "body": body}, "classification": {"topic": topic}}
    return h_team_route(st, {"_node_id": "r", "default": "support"})["routed_team"]


# --------------------------------------------------------------------------
# team_route
# --------------------------------------------------------------------------
def test_team_route_maps_intent_to_team():
    assert _route("How do I renew our contract?") == "csm"
    assert _route("What does the Team plan cost? send a quote") == "sales"
    assert _route("Please cancel our account and export all our data") == "offboarding"
    assert _route("My Zap trigger stopped firing") == "support"          # default
    # CSM is checked before Sales — an expansion question from a customer
    assert _route("We want to renew and add seats") == "csm"
    # topic slug counts too, not just the subject
    assert _route("hi", topic="contract-renewal") == "csm"


def test_team_route_trace_and_custom_rules():
    st = {"case": {"subject": "urgent: refund please"}, "classification": {"topic": "x"}}
    out = h_team_route(st, {"_node_id": "r", "default": "support",
                            "rules": [{"team": "billing", "any": ["refund"]}]})
    assert out["routed_team"] == "billing"
    assert "billing" in out["trace"][0]["summary"] and "refund" in out["trace"][0]["summary"]


# --------------------------------------------------------------------------
# _route_queue — the design-doc "team owns it" rule
# --------------------------------------------------------------------------
_CFG = {"queue_by_team": {"support": "Support_Tier2", "csm": "Team_CSM",
                          "sales": "Team_Sales", "offboarding": "Team_Offboarding"},
        "escalate_queue": "Billing_Escalations", "queue": "Support_Tier2"}


def test_route_queue_sends_routed_teams_to_their_own_queue():
    # a routed team keeps its case even on a billing-flavoured escalation
    st = {"routed_team": "sales",
          "confidence_gate": {"forced_escalation": "module 'Billing & Plans'"}}
    assert _route_queue(st, _CFG) == "Team_Sales"

    st = {"routed_team": "csm", "confidence_gate": {}}
    assert _route_queue(st, _CFG) == "Team_CSM"


def test_route_queue_support_billing_goes_to_the_billing_queue():
    st = {"routed_team": "support",
          "confidence_gate": {"forced_escalation": "topic 'x' ~ 'refund'"}}
    assert _route_queue(st, _CFG) == "Billing_Escalations"

    st = {"routed_team": "support", "confidence_gate": {"pass": False}}
    assert _route_queue(st, _CFG) == "Support_Tier2"           # non-billing -> team/static


def test_handover_enterprise_queue_beats_team(monkeypatch):
    seen = {}
    monkeypatch.setattr(salesforce, "assign_case",
                        lambda cid, **k: (seen.update(k) or {"assigned": True, "owner_type": "queue"}))
    out = h_handover(
        {"case": {"sf_id": "500H"}, "tier": "enterprise", "routed_team": "csm"},
        {"_node_id": "h", "reason": "x", "queue_by_team": _CFG["queue_by_team"],
         "enterprise_queue": "Enterprise_Support"},
    )
    assert seen["queue"] == "Enterprise_Support"
    assert out["outcome"]["assignment"]["assigned"] is True


# --------------------------------------------------------------------------
# the seeded flow
# --------------------------------------------------------------------------
def test_case_router_portable_flow_compiles_and_routes():
    p = pathlib.Path(__file__).resolve().parents[1] / "interpreter/flows/flow_case_router.json"
    flow = json.loads(p.read_text())
    assert check_flow(Flow.model_validate(flow), require_expected_types=False) == []
    build_graph(flow)
    types = [n["type"] for n in flow["nodes"]]
    assert types[:5] == ["identify", "sf_case", "retrieve", "classify", "team_route"]
    gate_edges = sorted(e["condition"]["if"] for e in flow["edges"]
                        if e.get("condition", {}).get("if"))
    assert any("routed_team == 'offboarding'" in c for c in gate_edges)
    assert any("tier == 'enterprise'" in c for c in gate_edges)
