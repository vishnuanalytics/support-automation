"""
Phase 20p — routing matrix for the comprehensive email sf_entry flow.

Walks the portable flow_email_l0l1.json through every team / tier / Case.Type
branch: run `team_route` + `confidence_gate` on a synthetic classification,
then evaluate the flow's real gate edges. No LLM / retrieval / Salesforce.
Mirrors scripts/run_scenarios.py.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import salesforce
from interpreter.builder import _context, build_graph
from interpreter.conditions import evaluate
from interpreter.flows.validate_flow import Flow, check_flow
from interpreter.registry import h_confidence_gate, h_team_route

_HERMETIC = ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN", "SF_CONSUMER_KEY",
             "SF_CONSUMER_SECRET", "SF_PRIVATE_KEY", "SF_PRIVATE_KEY_FILE",
             "GROQ_API_KEY", "ANTHROPIC_API_KEY")
for _k in _HERMETIC:
    os.environ.pop(_k, None)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    for k in _HERMETIC:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(salesforce, "_client_obj", None, raising=False)


_FLOW = json.loads(
    (pathlib.Path(__file__).resolve().parents[1] / "interpreter/flows/flow_email_l0l1.json").read_text()
)
_BY_ID = {n["node_id"]: n for n in _FLOW["nodes"]}
_GATE = next(n for n in _FLOW["nodes"] if n["type"] == "confidence_gate")
_ROUTE = next(n for n in _FLOW["nodes"] if n["type"] == "team_route")
_GATE_EDGES = [e for e in _FLOW["edges"] if e["source_node_id"] == _GATE["node_id"]]


def _land(topic: str, tier: str, retrieval_score: float) -> tuple[str, str, bool]:
    state = {
        "case": {"subject": topic, "body": topic},
        "classification": {"topic": topic},
        "tier": tier,
        "retrieval_score": retrieval_score,
        "draft_confidence": 0.95,
        "groundedness": {"score": retrieval_score},
    }
    state.update(h_team_route(state, {**(_ROUTE.get("config") or {}), "_node_id": "r"}))
    state.update(h_confidence_gate(state, {**(_GATE.get("config") or {}), "_node_id": "g"}))
    ctx = _context(state)
    for e in _GATE_EDGES:
        if evaluate(e["condition"]["if"], ctx):
            return _BY_ID[e["target_node_id"]]["type"], state["routed_team"], state["confidence_gate"]["pass"]
    raise AssertionError(f"no gate edge matched for topic={topic!r} tier={tier}")


def test_flow_compiles_and_gate_is_exhaustive():
    assert check_flow(Flow.model_validate(_FLOW), require_expected_types=False) == []
    build_graph(_FLOW)
    assert len(_GATE_EDGES) == 5


@pytest.mark.parametrize(
    "label, topic, tier, rscore, expect",
    [
        ("how-to KB-covered / basic",      "zap-activation",             "basic",      0.92, "auto_reply"),
        ("vague no detail / basic",        "unclear",                    "basic",      0.10, "clarify"),
        ("billing double charge / basic",  "billing-refund",             "basic",      0.20, "notify"),
        ("account-login SSO / basic",      "account-access-sso",         "basic",      0.20, "notify"),
        ("bug KB-thin / basic",            "webhook-error",              "basic",      0.15, "clarify"),
        ("renewal + seats / premium",      "contract-renewal",           "premium",    0.30, "ask_human"),
        ("pre-sales pricing / basic",      "pricing-quote",              "basic",      0.30, "ask_human"),
        ("cancellation + GDPR / premium",  "cancellation-data-export",   "premium",    0.30, "handover"),
        ("enterprise tier / any",          "trigger-timezone",           "enterprise", 0.90, "handover"),
        ("how-to KB-covered / premium",    "zap-filter-step",            "premium",    0.93, "auto_reply"),
    ],
)
def test_scenario_routes_as_expected(label, topic, tier, rscore, expect):
    landed, _team, _passed = _land(topic, tier, rscore)
    assert landed == expect, f"{label}: landed {landed}, want {expect}"


def test_team_routes_go_to_their_owner():
    assert _land("contract-renewal", "basic", 0.9)[1] == "csm"
    assert _land("pricing-quote", "basic", 0.9)[1] == "sales"
    assert _land("cancel my account", "basic", 0.9)[1] == "offboarding"
    assert _land("how do I filter a zap", "basic", 0.9)[1] == "support"
    # enterprise beats a routed team
    assert _land("contract-renewal", "enterprise", 0.9)[0] == "handover"
