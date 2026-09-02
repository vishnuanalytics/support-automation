"""P6a — flow triggers. Offline: the `webhook_context` shaping + `_trigger_view`.
Integration: mint a webhook token and fire `/t/<token>` against the live DB."""

from __future__ import annotations

import os
import pathlib
import sys
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def test_trigger_view_builds_a_webhook_url(monkeypatch):
    from api import main
    monkeypatch.setenv("PUBLIC_API_BASE", "https://api.example.com/")
    v = main._trigger_view({"trigger_id": "t1", "kind": "webhook", "token": "abc",
                            "fire_count": 3, "enabled": True})
    assert v["url"] == "https://api.example.com/t/abc"
    assert "token" not in v          # the raw token is only inside the URL
    v2 = main._trigger_view({"trigger_id": "t2", "kind": "schedule", "cron": "*/5 * * * *"})
    assert "url" not in v2 and v2["cron"] == "*/5 * * * *"


# ── integration ──────────────────────────────────────────────────────
pytestmark_int = pytest.mark.skipif(
    not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY")),
    reason="no SUPABASE creds")


@pytest.mark.integration
@pytestmark_int
def test_public_webhook_fires_a_run():
    from api.main import _service, app
    from fastapi.testclient import TestClient

    ten = str(uuid.uuid4())
    flow = (_service.table("flows").insert(
        {"tenant_id": ten, "team": "support", "name": "p6a-int"}).execute().data[0])
    trg = (_service.table("flow_triggers").insert(
        {"flow_id": flow["flow_id"], "tenant_id": ten, "kind": "webhook",
         "token": "tok_" + uuid.uuid4().hex}).execute().data[0])
    job_id = None
    try:
        r = TestClient(app).post(f"/t/{trg['token']}", json={"query": "reset my token"})
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        assert job_id
        job = _service.table("jobs").select("kind, payload").eq("job_id", job_id).execute().data[0]
        assert job["kind"] == "run_flow"
        assert job["payload"]["context"]["query"] == "reset my token"
        assert job["payload"]["flow_id"] == flow["flow_id"]
        fresh = _service.table("flow_triggers").select("fire_count") \
            .eq("trigger_id", trg["trigger_id"]).execute().data[0]
        assert fresh["fire_count"] == 1
    finally:
        if job_id:
            _service.table("jobs").delete().eq("job_id", job_id).execute()
        _service.table("flow_triggers").delete().eq("trigger_id", trg["trigger_id"]).execute()
        _service.table("flows").delete().eq("flow_id", flow["flow_id"]).execute()
