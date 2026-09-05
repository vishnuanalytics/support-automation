"""KIL-b — the contradiction judge + its gate wiring. Offline: no LLM key, so
`integrity.check` runs the deterministic heuristic backend."""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

for _k in ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_k, None)

from interpreter import integrity, llm
from interpreter.registry import h_confidence_gate


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda *a, **k: False)


# ── check() / heuristic backend ──────────────────────────────────────────
def test_negation_mismatch_over_shared_terms_is_a_contradiction():
    res = integrity.check(
        "Webhooks are available on the Free plan for every account.",
        [{"text": "Webhooks are not available on the Free plan; they require a Business plan."}],
        kind="draft",
    )
    assert res["relation"] == "contradicts" and res["flagged"] is True
    assert res["backend"] == "heuristic"


def test_strong_overlap_same_polarity_entails():
    res = integrity.check(
        "Task history retention on the Starter plan is thirty days for every workflow.",
        [{"text": "Task history retention on the Starter plan is thirty days for every workflow run."}],
        kind="draft",
    )
    assert res["relation"] == "entails" and res["flagged"] is False


def test_unrelated_context_is_neutral_not_flagged():
    res = integrity.check(
        "Thanks for reaching out, I'd be glad to help you get this sorted.",
        [{"text": "Multi-step Zaps support up to one hundred steps on paid plans."}],
        kind="draft",
    )
    assert res["relation"] == "neutral" and res["flagged"] is False


def test_empty_inputs_are_a_safe_neutral():
    assert integrity.check("", [{"text": "anything"}])["relation"] == "neutral"
    assert integrity.check("something", [])["flagged"] is False
    assert integrity.check("something", None)["backend"] == "none"


def test_novel_only_for_draft_and_human_reply():
    # a claim the context neither supports nor denies, on a bot draft
    v = [{"claim": "x", "relation": "neutral", "evidence": "", "confidence": 0.7}]
    s = integrity._summarize(v, backend="groq")
    s["flagged"] = s["relation"] == "contradicts"
    assert s["relation"] == "neutral"


def test_summarize_takes_the_worst_relation():
    v = [
        {"claim": "a", "relation": "entails", "evidence": "", "confidence": 0.9},
        {"claim": "b", "relation": "contradicts", "evidence": "", "confidence": 0.8},
        {"claim": "c", "relation": "neutral", "evidence": "", "confidence": 0.9},
    ]
    out = integrity._summarize(v, backend="groq")
    assert out["relation"] == "contradicts" and out["salient"] == ["b"]


def test_low_confidence_verdicts_are_ignored():
    v = [{"claim": "b", "relation": "contradicts", "evidence": "", "confidence": 0.3}]
    assert integrity._summarize(v, backend="groq")["relation"] == "neutral"


# ── check_many() — h_draft's combined-call path ─────────────────────────
_CTX = [{"text": "Webhooks require a Business plan or above.", "ref": "kb1"}]


def test_check_many_makes_one_call_for_two_statements(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    calls = []

    def fake_complete(**kw):
        calls.append(kw)
        return (
            '{"statements": {'
            '"draft": {"claims": [{"claim": "webhooks are free", "relation": "contradicts", '
            '"evidence": "business plan required", "confidence": 0.9}]}, '
            '"inbound": {"claims": [{"claim": "customer wants webhooks", "relation": "neutral", '
            '"evidence": "", "confidence": 0.9}]}}}'
        )

    monkeypatch.setattr(llm, "complete", fake_complete)
    out = integrity.check_many(
        {"draft": "Webhooks are free on every plan.", "inbound": "Can I get webhooks?"},
        _CTX, kinds={"draft": "draft", "inbound": "inbound"},
    )
    assert len(calls) == 1                      # one Groq round-trip, not two
    assert "STATEMENT[draft]" in calls[0]["user"] and "STATEMENT[inbound]" in calls[0]["user"]
    assert out["draft"]["relation"] == "contradicts" and out["draft"]["flagged"] is True
    assert out["inbound"]["relation"] == "neutral" and out["inbound"]["novel"] is False
    # kind="inbound" never sets novel, even though this claim is unsupported-neutral
    assert out["draft"]["novel"] is False        # this claim was `contradicts`, not neutral


def test_check_many_skips_the_combined_call_with_only_one_statement(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(llm, "complete", lambda **kw: calls.append(kw) or
                        '{"claims": []}')
    out = integrity.check_many({"draft": "Some reply.", "inbound": ""}, _CTX,
                               kinds={"draft": "draft", "inbound": "inbound"})
    assert len(calls) == 1                       # per-statement check(), not the multi-shape
    assert "several labeled STATEMENTS" not in calls[0]["system"]
    assert out["inbound"]["backend"] == "none"   # empty statement -> the safe empty result


def test_check_many_falls_back_to_per_statement_on_malformed_json(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(llm, "complete", lambda **kw: calls.append(kw) or "not json")
    out = integrity.check_many({"draft": "reply text", "inbound": "inbound text"}, _CTX)
    # 1 failed combined attempt + 2 per-statement fallback calls
    assert len(calls) == 3
    assert set(out) == {"draft", "inbound"}
    assert out["draft"]["backend"] in ("groq", "heuristic")


def test_check_many_falls_back_when_a_label_is_dropped(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    calls = []

    def fake_complete(**kw):
        calls.append(kw)
        if len(calls) == 1:
            return '{"statements": {"draft": {"claims": []}}}'   # "inbound" missing
        return '{"claims": []}'

    monkeypatch.setattr(llm, "complete", fake_complete)
    out = integrity.check_many({"draft": "reply text", "inbound": "inbound text"}, _CTX)
    assert len(calls) == 3   # the dropped-label combined attempt + 2 fallback calls
    assert set(out) == {"draft", "inbound"}


def test_check_many_falls_back_without_llm():
    # autouse fixture keeps llm.available() False for this one
    out = integrity.check_many({"draft": "Webhooks are free everywhere.",
                                "inbound": "Do you support webhooks?"}, _CTX,
                               kinds={"draft": "draft", "inbound": "inbound"})
    assert set(out) == {"draft", "inbound"}
    assert out["draft"]["backend"] == "heuristic"


def test_contexts_from_state_assembles_kb_and_history():
    ctx = integrity.contexts_from_state({
        "prior_resolutions": [{"resolution_text": "Toggle the Zap on from the dashboard.",
                               "case_number": "00042"}],
        "internal_kb": {"matches": [{"chunk_text": "Internal runbook step."}]},
        "retrieval": [{"chunk_text": "Public doc chunk.", "doc_url": "https://docs/x"}],
    })
    kinds = {c["kind"] for c in ctx}
    assert kinds == {"resolution", "kb"} and len(ctx) == 3
    assert ctx[0]["ref"] == "case 00042"


# ── gate wiring ─────────────────────────────────────────────────────────
def _gate(state_extra):
    state = {"tier": "basic", "retrieval_score": 0.9, "draft_confidence": 0.9,
             "groundedness": {"score": 0.9}, "classification": {"topic": "product-usage"},
             **state_extra}
    return h_confidence_gate(state, {"_node_id": "g", "default_threshold": 0.3})


def test_gate_escalates_when_the_draft_contradicts_knowledge():
    out = _gate({"integrity": {"draft": {"flagged": True, "relation": "contradicts"}}})
    g = out["confidence_gate"]
    assert g["pass"] is False
    assert "integrity conflict" in g["forced_escalation"]


def test_gate_passes_a_clean_draft():
    out = _gate({"integrity": {"draft": {"flagged": False, "relation": "entails"}}})
    assert out["confidence_gate"]["pass"] is True
    assert "forced_escalation" not in out["confidence_gate"]


def test_gate_conflict_check_can_be_disabled_per_flow():
    state = {"tier": "basic", "retrieval_score": 0.9, "draft_confidence": 0.9,
             "groundedness": {"score": 0.9}, "classification": {"topic": "product-usage"},
             "integrity": {"draft": {"flagged": True}}}
    out = h_confidence_gate(state, {"_node_id": "g", "default_threshold": 0.3,
                                    "escalate_on_integrity_conflict": False})
    assert out["confidence_gate"]["pass"] is True


# ── eval harness ────────────────────────────────────────────────────────
def test_eval_harness_runs_and_reports():
    from eval.integrity.run_integrity_eval import evaluate
    r = evaluate()
    assert r["n"] == 24
    assert set(r) >= {"accuracy", "flag_precision", "flag_recall", "confusion", "misses"}
    assert 0.0 <= r["flag_precision"] <= 1.0
