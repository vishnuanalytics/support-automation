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


def test_fire_schedules_enqueues_a_due_trigger(monkeypatch):
    from interpreter import sweeps
    from datetime import datetime, timezone

    monkeypatch.setattr(sweeps, "_now", lambda: datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc))
    enq = []
    monkeypatch.setattr("interpreter.jobs.enqueue",
                        lambda kind, payload, **kw: enq.append((kind, payload, kw)))

    updated = []

    class _T:
        def __init__(self, rows): self.rows = rows
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def limit(self, n): return self
        def execute(self): return type("R", (), {"data": self.rows})
        def update(self, p): updated.append(p); return self

    rows = [
        {"trigger_id": "s1", "flow_id": "f1", "cron": "0 9 * * *",  # due at 09:00
         "last_fired_at": "2026-09-02T08:00:00Z", "fire_count": 4, "enabled": True},
        {"trigger_id": "s2", "flow_id": "f2", "cron": "0 10 * * *",  # not due
         "last_fired_at": None, "fire_count": 0, "enabled": True},
        {"trigger_id": "s3", "flow_id": "f3", "cron": "0 9 * * *",   # already fired this minute
         "last_fired_at": "2026-09-02T09:00:00Z", "fire_count": 1, "enabled": True},
    ]
    sb = type("SB", (), {"table": lambda self, n: _T(rows)})()
    out = sweeps.fire_schedules(sb, dry_run=False)

    assert out["fired"] == ["f1"]
    assert len(enq) == 1 and enq[0][0] == "run_flow"
    assert enq[0][1]["flow_id"] == "f1"
    assert enq[0][1]["context"]["_trigger"] == "schedule"
    assert enq[0][2]["dedupe_key"] == "sched:s1:202609020900"
    assert updated and updated[0]["fire_count"] == 5


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
