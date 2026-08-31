"""
Phase 22 — the Case timeline builder (api/trace.py). Pure; no DB.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api.trace import build_timeline, render_markdown

_NOW = datetime.now(timezone.utc)


def _run(**kw):
    base = dict(
        run_id="r1", flow_id="e5e5e5e5-x", flow_version=7, source="cdc",
        case_id="500X", idempotency_key="k1", outcome="notify", confidence=0.42,
        human_action=None, human_reply=None, gate={"pass": False, "score": 0.42},
        case_payload={"sf_id": "500X", "case_number": "00001234"},
        created_at=(_NOW - timedelta(minutes=5)).isoformat(),
        trace=[
            {"type": "sf_case", "summary": "created Case 00001234", "data": {"elapsed_ms": 800}},
            {"type": "classify", "summary": "topic=billing type=Billing mode=informational",
             "data": {"elapsed_ms": 1200, "stub": True, "tokens": {"total": 0}}},
            {"type": "sf_writeback", "summary": "wrote Type, Priority",
             "data": {"elapsed_ms": 300, "written": {"Type": "Billing", "Priority": "Medium"},
                      "skipped": {"Module__c": "no keyword match"}}},
            {"type": "confidence_gate", "summary": "forced escalate (topic 'billing')",
             "data": {"elapsed_ms": 5, "pass": False, "forced_escalation": "topic 'billing'"}},
            {"type": "notify", "summary": "Chatter posted -> Billing team",
             "data": {"elapsed_ms": 400, "assignment": {}, "label": "Billing team"}},
        ],
    )
    base.update(kw)
    return base


def _job(**kw):
    base = dict(job_id="j1", kind="run_flow", status="done", attempts=1, max_attempts=3,
                error=None, dedupe_key="sfcase:500X", run_after=None, locked_at=None,
                created_at=(_NOW - timedelta(minutes=6)).isoformat(),
                updated_at=(_NOW - timedelta(minutes=5)).isoformat())
    base.update(kw)
    return base


def test_timeline_orders_and_summarises():
    t = build_timeline(key="00001234", runs=[_run()], jobs=[_job()])
    assert t["case_number"] == "00001234" and t["sf_id"] == "500X"
    assert t["outcome"] == "notify"
    assert t["degraded_llm"] is True                      # classify ran stubbed
    assert t["labels_written"] == {"Type": "Billing", "Priority": "Medium"}
    assert t["labels_skipped"] == {"Module__c": "no keyword match"}
    assert t["final_queue"] == "Billing team"
    kinds = [e["kind"] for e in t["timeline"]]
    assert kinds[0] == "job"                              # earliest
    assert "run_start" in kinds and "run_end" in kinds
    assert kinds.count("node") == 5
    # nodes are ordered by cumulative elapsed_ms within the run
    node_ts = [e["ts"] for e in t["timeline"] if e["kind"] == "node"]
    assert node_ts == sorted(node_ts)


def test_flags_failed_and_stale_jobs():
    stale = _job(job_id="j2", status="running", attempts=3,
                 locked_at=(_NOW - timedelta(minutes=30)).isoformat())
    failed = _job(job_id="j3", status="failed", attempts=3,
                  error="RateLimitError: 429 tokens per day")
    t = build_timeline(key="500X", runs=[], jobs=[stale, failed])
    assert "j2" in t["stale_jobs"]
    assert "j3" in t["failed_jobs"]
    assert any("429" in e for e in t["errors"])
    assert t["counts"]["runs"] == 0


def test_node_error_surfaces():
    r = _run(trace=[{"type": "sf_case", "summary": "no sf_id on case",
                     "data": {"elapsed_ms": 5, "status": "no sf_id on case"}}])
    t = build_timeline(key="x", runs=[r], jobs=[])
    assert any("sf_case" in e and "no sf_id" in e for e in t["errors"])


def test_markdown_render_has_the_key_facts():
    md = render_markdown(build_timeline(key="00001234", runs=[_run()], jobs=[_job()]))
    assert "outcome: **notify**" in md
    assert "STUB mode" in md
    assert "labels written" in md and "labels skipped" in md
    assert "## timeline" in md
