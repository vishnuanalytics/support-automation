"""
Phase 11 — the resolution loop, end to end against the live Salesforce org
+ Supabase. Integration; skipped without SUPABASE_URL / SF creds.

Seeds a Case, runs the Acme support flow (enterprise tier -> handover, so
human_action = pending), posts a "human reply" as an outbound EmailMessage,
runs the check_resolution job, and asserts the run is scored.
"""

from __future__ import annotations

import os
import uuid

import pytest
from dotenv import load_dotenv

load_dotenv()
os.environ["RUNS_DISABLED"] = ""

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("SUPABASE_URL"), reason="no SUPABASE_URL"),
]

ACME_SUPPORT = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def sf_available():
    from interpreter import salesforce
    if not salesforce.available():
        pytest.skip("no Salesforce creds")
    return salesforce


@pytest.fixture
def sb():
    from ingestion.scraper import get_supabase
    return get_supabase()


def test_resolution_check_scores_the_human_reply(sf_available, sb):
    sf = sf_available._client()
    # a Case that will hand over (enterprise tier)
    acc = sf.Account.create({"Name": f"pytest-hitl-{uuid.uuid4().hex[:6]}", "Tier__c": "enterprise"})["id"]
    cid = sf.Case.create({"Subject": "hitl webhook question", "Description": "how do I test a webhook",
                          "AccountId": acc, "Status": "New", "Origin": "Web"})["id"]
    run_id = None
    try:
        from api.worker import _check_resolution, _run_flow
        res = _run_flow({"flow_id": ACME_SUPPORT,
                         "case": {"sf_id": cid, "subject": "hitl webhook question",
                                  "body": "how do I test a webhook",
                                  "account": {"customer_type": "enterprise"}}}, sb)
        run_id = res["run_id"]
        row = sb.table("runs").select("human_action, draft").eq("run_id", run_id).execute().data[0]
        assert row["human_action"] == "pending"

        # the "human" sends a lightly-edited version of the draft
        human = "Hi — " + (row["draft"] or "here are the steps") + "\n\nBest, Support"
        sf.EmailMessage.create({"ParentId": cid, "Incoming": False,
                                "Subject": "Re: hitl", "TextBody": human,
                                "ToAddress": "customer@example.test"})

        out = _check_resolution({"run_id": run_id}, sb)
        assert out["human_action"] in ("sent_as_is", "edited", "rewrote")
        scored = sb.table("runs").select("human_action, edit_distance, human_reply, feedback_checked_at") \
            .eq("run_id", run_id).execute().data[0]
        assert scored["human_action"] != "pending"
        assert scored["feedback_checked_at"] and scored["human_reply"]
        assert 0.0 <= float(scored["edit_distance"]) <= 1.0
    finally:
        if run_id:
            sb.table("runs").delete().eq("run_id", run_id).execute()
            sb.table("jobs").delete().eq("dedupe_key", run_id).execute()
        sf.Case.delete(cid)
        sf.Account.delete(acc)
