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
from interpreter.registry import h_sf_case

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
