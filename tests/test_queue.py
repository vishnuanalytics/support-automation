"""
Phase 10 — job queue, worker, idempotency. All integration (Supabase +
real retrieval); skipped without SUPABASE_URL.

    pytest tests/test_queue.py
"""

from __future__ import annotations

import os
import uuid

import pytest
from dotenv import load_dotenv

load_dotenv()
os.environ["RUNS_DISABLED"] = ""  # these tests want runs recorded

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("SUPABASE_URL"), reason="no SUPABASE_URL"),
]

GLOBEX_FLOW = "a2a2a2a2-2222-4222-8222-222222222222"


@pytest.fixture
def sb():
    from ingestion.scraper import get_supabase
    return get_supabase()


@pytest.fixture
def key_and_cleanup(sb):
    key = f"pytest-{uuid.uuid4().hex[:10]}"
    yield key
    sb.table("jobs").delete().eq("dedupe_key", key).execute()
    sb.table("runs").delete().eq("idempotency_key", key).execute()


def _case(key: str) -> dict:
    return {"case_id": key, "subject": "webhook help", "body": "how do I test a webhook",
            "account": {"customer_type": "premium"}}


def test_enqueue_dedupes_a_live_job(sb, key_and_cleanup):
    from interpreter import jobs
    p = {"flow_id": GLOBEX_FLOW, "case": _case(key_and_cleanup), "idempotency_key": key_and_cleanup}
    j1 = jobs.enqueue("run_flow", p, dedupe_key=key_and_cleanup, sb=sb)
    j2 = jobs.enqueue("run_flow", p, dedupe_key=key_and_cleanup, sb=sb)
    assert j1 and j2 is None


def test_worker_runs_the_job_and_records_the_run(sb, key_and_cleanup):
    from api.worker import process_one
    from interpreter import jobs

    key = key_and_cleanup
    jobs.enqueue("run_flow",
                 {"flow_id": GLOBEX_FLOW, "case": _case(key), "idempotency_key": key},
                 dedupe_key=key, sb=sb)
    assert process_one(sb) is True

    run = sb.table("runs").select("source, flow_version, outcome") \
        .eq("idempotency_key", key).execute().data
    assert len(run) == 1
    assert run[0]["source"] == "worker" and run[0]["flow_version"] == 1
    assert run[0]["outcome"] in ("auto_reply", "ask_human", "handover")


def test_second_job_same_key_does_not_double_run(sb, key_and_cleanup):
    from api.worker import process_one
    from interpreter import jobs

    key = key_and_cleanup
    p = {"flow_id": GLOBEX_FLOW, "case": _case(key), "idempotency_key": key}
    jobs.enqueue("run_flow", p, dedupe_key=key, sb=sb)
    process_one(sb)                                   # first job -> records a run
    jobs.enqueue("run_flow", p, dedupe_key=key, sb=sb)  # allowed (prev job is done)
    process_one(sb)                                   # worker sees the dup -> skips

    runs = sb.table("runs").select("run_id").eq("idempotency_key", key).execute().data
    assert len(runs) == 1
