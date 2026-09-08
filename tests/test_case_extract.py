"""interpreter/case_extract.py — root-cause / integration extraction from a
resolved case's text. Offline: `llm.complete` is stubbed."""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import case_extract as ce


def _stub(monkeypatch, payload):
    monkeypatch.setattr("interpreter.llm.complete", lambda *a, **k: json.dumps(payload))


def test_snaps_integrations_to_vocab_and_caps_root_cause(monkeypatch):
    _stub(monkeypatch, {"root_cause": "  " + "x" * 200,
                        "integrations": ["SalesForce", "zomato ordering", "acme-crm"]})
    out = ce.extract_case_signals("Sync broken", "orders not syncing", "rotated the token")
    assert len(out["root_cause"]) == ce._ROOT_CAUSE_MAX
    # "SalesForce" -> Salesforce, "zomato ordering" -> Zomato (substring hit),
    # "acme-crm" not in the vocab -> dropped
    assert out["integrations"] == ["Salesforce", "Zomato"]


def test_null_root_cause_and_empty_list(monkeypatch):
    _stub(monkeypatch, {"root_cause": None, "integrations": []})
    assert ce.extract_case_signals("s", "b", "r") == {"root_cause": None, "integrations": []}


def test_no_text_never_calls_the_model(monkeypatch):
    seen = {"n": 0}
    monkeypatch.setattr("interpreter.llm.complete",
                        lambda *a, **k: seen.__setitem__("n", seen["n"] + 1) or "{}")
    assert ce.extract_case_signals(None, None, None) == {"root_cause": None, "integrations": []}
    assert seen["n"] == 0


def test_model_failure_is_swallowed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("groq down")

    monkeypatch.setattr("interpreter.llm.complete", boom)
    assert ce.extract_case_signals("s", "b", "r") == {"root_cause": None, "integrations": []}


def test_bad_json_is_swallowed(monkeypatch):
    monkeypatch.setattr("interpreter.llm.complete", lambda *a, **k: "not json")
    assert ce.extract_case_signals("s", "b", "r") == {"root_cause": None, "integrations": []}
