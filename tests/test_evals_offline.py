"""P8c — the KIL eval harnesses run offline (deterministic backend).

These don't assert a score bar (that needs the Groq judge and moves); they
guard the harness plumbing — cases parse, the runner completes, the report
shape is stable.
"""

from __future__ import annotations

import importlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import llm


def test_review_eval_runs_and_reports(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: False)
    mod = importlib.import_module("eval.review.run_review_eval")
    r = mod.evaluate()
    assert r["n"] >= 12 and r["backend"] == "heuristic"
    for k in ("accuracy", "flag_precision", "flag_recall", "flag_f1"):
        assert 0.0 <= r[k] <= 1.0
    c = r["confusion"]
    assert c["tp"] + c["fp"] + c["tn"] + c["fn"] == r["n"]


def test_writeback_eval_runs_and_reports(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: False)
    mod = importlib.import_module("eval.writeback.run_writeback_eval")
    r = mod.evaluate()
    assert r["n"] >= 10 and r["backend"] == "heuristic"
    # fallback draft = the statement itself, so every valid case resolves
    assert r["resolution_rate"] == 1.0
    assert r["mean_answer_lift"] > 0.0          # corrected body carries the gold keywords
    assert len(r["per_case"]) == r["n"]


def test_writeback_cases_are_wellformed():
    import json

    p = pathlib.Path(__file__).resolve().parents[1] / "eval/writeback/cases.jsonl"
    for ln in p.read_text().splitlines():
        if not ln.strip():
            continue
        c = json.loads(ln)
        assert {"id", "stale_kb", "statement", "question", "gold_keywords"} <= c.keys()
        assert c["gold_keywords"]
