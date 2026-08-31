"""
Phase 21 — Case-resolution memory: redaction / generalisable heuristic,
`lookup` ranking + the pattern-vs-proof split, and the `case_lookup` node.
Offline: the embedder, Supabase RPC, and Neo4j are all stubbed.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import case_memory, salesforce
from interpreter.registry import h_case_lookup, h_classify

_HERMETIC = ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN", "SF_CONSUMER_KEY",
             "SF_CONSUMER_SECRET", "SF_PRIVATE_KEY", "SF_PRIVATE_KEY_FILE",
             "GROQ_API_KEY", "ANTHROPIC_API_KEY", "NEO4J_URI")
for _k in _HERMETIC:
    os.environ.pop(_k, None)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    for k in _HERMETIC:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(salesforce, "_client_obj", None, raising=False)
    # deterministic 4-d "embedding"
    monkeypatch.setattr(case_memory, "embed", lambda t: [0.1, 0.2, 0.3, 0.4])


# --------------------------------------------------------------------------
# pattern vs proof
# --------------------------------------------------------------------------
def test_looks_specific():
    assert case_memory.looks_specific(
        "Your endpoint returned 200 in 14.2s at 2026-08-31T14:03 for request 9982137") is True
    assert case_memory.looks_specific(
        "Invoice 4471902 was charged $49.00 on 2026-08-01") is True
    assert case_memory.looks_specific(
        "Turn on the Zap from the dashboard, then publish it — filters run top to bottom.") is False
    assert case_memory.looks_specific("") is False


def test_redact_masks_identifiers():
    r = case_memory.redact("Contact a@b.com, invoice 88123456, at 2026-08-31T09:00")
    assert "a@b.com" not in r and "88123456" not in r and "2026-08-31T09:00" not in r
    assert "<email>" in r and "<num>" in r and "<timestamp>" in r


def test_classify_resolution_kind():
    assert case_memory.classify_resolution_kind("edited", "some reply") == "agent_edit_of_bot"
    assert case_memory.classify_resolution_kind("sent", "the bot draft", from_bot=True) == "auto_reply_accepted"
    assert case_memory.classify_resolution_kind(None, "This is a known issue, we're aware.") == "known_issue"
    assert case_memory.classify_resolution_kind(
        None, "We checked your logs: request 771281 timed out at 2026-08-31T10:00.") == "diagnostic_finding"
    assert case_memory.classify_resolution_kind("no_reply", "") == "no_fix"


# --------------------------------------------------------------------------
# lookup — ranking + the citable / hints split
# --------------------------------------------------------------------------
class _FakeSB:
    def __init__(self, rows):
        self._rows = rows

    def rpc(self, name, args):
        assert name == "match_case_memory"
        self._last = args
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


def _row(**kw):
    base = dict(case_sf_id="x", case_number="00001", subject="s", body_summary="b",
                case_type="Problem / Bug", module="API & Webhooks", tier="basic",
                resolution_kind="agent_reply", resolution_text="Return 2xx within 10s.",
                generalizable=True, resolved_at=None, similarity=0.6)
    base.update(kw)
    return base


def test_lookup_boosts_and_splits(monkeypatch):
    monkeypatch.setattr(case_memory, "_graph_duplicates", lambda ids: set())
    rows = [
        _row(case_sf_id="a", case_number="A", similarity=0.60,
             case_type="Problem / Bug", module="API & Webhooks", tier="basic"),   # all boosts
        _row(case_sf_id="b", case_number="B", similarity=0.70,
             case_type="Question", module="Zaps", tier="enterprise"),             # higher sim, no boost
        _row(case_sf_id="c", case_number="C", similarity=0.55,
             generalizable=False,
             resolution_text="We saw your account 5567123 fail at 2026-08-01T00:00."),  # not citable
        _row(case_sf_id="d", case_number="D", similarity=0.20),                    # below min_similarity
    ]
    out = case_memory.lookup(_FakeSB(rows), "webhook 500", tenant_id="t",
                             case_type="Problem / Bug", module="API & Webhooks",
                             tier="basic", k=3, min_similarity=0.35)
    cited = [c["case_number"] for c in out["citable"]]
    assert cited[0] == "A"                       # 0.60 + 0.15 + 0.10 + 0.05 = 0.90 > B's 0.70
    assert "B" in cited
    assert "C" not in cited                      # non-generalisable -> hints only
    assert "D" not in cited                      # filtered by min_similarity
    assert any("Case C" in h for h in out["hints"])


def test_lookup_duplicate_edge_wins(monkeypatch):
    monkeypatch.setattr(case_memory, "_graph_duplicates", lambda ids: {"b"})
    rows = [_row(case_sf_id="a", case_number="A", similarity=0.60),
            _row(case_sf_id="b", case_number="B", similarity=0.45)]
    out = case_memory.lookup(_FakeSB(rows), "q", tenant_id="t", k=2, use_graph=True)
    assert out["citable"][0]["case_number"] == "B"   # 0.45 + 0.30 dup boost
    assert out["citable"][0]["duplicate"] is True


def test_lookup_empty_when_no_rows():
    out = case_memory.lookup(_FakeSB([]), "q", tenant_id="t")
    assert out == {"citable": [], "hints": [], "scanned": 0}


# --------------------------------------------------------------------------
# the case_lookup node
# --------------------------------------------------------------------------
def test_case_lookup_skips_action_mode():
    out = h_case_lookup(
        {"classification": {"answer_mode": "action"}, "tenant_id": "t"},
        {"_node_id": "cl"},
    )
    assert out["prior_resolutions"] == [] and "skipped (answer_mode=action)" in out["trace"][0]["summary"]


def test_case_lookup_skips_when_memory_thin(monkeypatch):
    monkeypatch.setattr(case_memory, "count_for_tenant", lambda sb, t: 1)
    out = h_case_lookup(
        {"classification": {"answer_mode": "informational", "summary": "x"},
         "case": {"subject": "s", "body": "b"}, "tenant_id": "t"},
        {"_node_id": "cl", "_sb": _FakeSB([]), "min_memories": 3},
    )
    assert "skipped (<3 memories" in out["trace"][0]["summary"]


def test_case_lookup_diagnostic_forces_hints_only(monkeypatch):
    monkeypatch.setattr(case_memory, "count_for_tenant", lambda sb, t: 9)
    monkeypatch.setattr(case_memory, "lookup", lambda *a, **k: {
        "citable": [{"subject": "past bug", "resolution_text": "Return 2xx fast.",
                     "case_number": "P", "kind": "agent_reply", "duplicate": False,
                     "relevance": 0.9}],
        "hints": ["Case Q: check auth"], "scanned": 5,
    })
    out = h_case_lookup(
        {"classification": {"answer_mode": "diagnostic", "summary": "why did my zap fail",
                            "case_type": "Problem / Bug", "topic": "zap-run"},
         "case": {"subject": "s", "body": "b"}, "tenant_id": "t"},
        {"_node_id": "cl", "_sb": _FakeSB([])},
    )
    assert out["prior_resolutions"] == []                 # diagnostic never quotes memory
    assert any("past bug" in h for h in out["investigation_hints"])


# --------------------------------------------------------------------------
# classify answer_mode (deterministic fallback / stub)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text, mode", [
    ("Please cancel our account and export all our data", "action"),
    ("Why did my Zap not run this morning?", "diagnostic"),
    ("Is this a known issue? the API seems down", "status"),
    ("How do I add a filter step to my Zap?", "informational"),
])
def test_classify_answer_mode_fallback(text, mode):
    out = h_classify({"case": {"subject": "", "body": text}},
                     {"_node_id": "c", "default_tier": "basic"})
    assert out["classification"]["answer_mode"] == mode
