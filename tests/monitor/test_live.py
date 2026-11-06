"""Case Monitor against the real database: queries, lock and RLS."""

import pytest

pytestmark = pytest.mark.integration

GLOBEX = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def sb():
    from interpreter.supabase_client import make_client
    import os
    from dotenv import load_dotenv
    load_dotenv(override=True)
    if not os.environ.get("SUPABASE_SERVICE_KEY"):
        pytest.skip("needs Supabase credentials")
    return make_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


@pytest.fixture
def tenant(sb):
    from interpreter.monitor import service
    service.clear_demo(sb, GLOBEX)
    (sb.table("monitor_syncs").delete().eq("tenant_id", GLOBEX).execute())
    yield GLOBEX
    service.clear_demo(sb, GLOBEX)
    sb.table("monitor_reviews").delete().eq("tenant_id", GLOBEX).execute()
    sb.table("monitor_syncs").delete().eq("tenant_id", GLOBEX).execute()


def test_demo_runs_through_real_tables(sb, tenant):
    from interpreter.monitor import service

    assert service.load_demo(sb, tenant)["loaded"] == 9
    q = service.queue(sb, tenant)
    assert q["tiles"]["total"] == 9
    assert q["tiles"]["my_turn"] == sum(c["ball"] == "us" for c in q["cases"])
    assert [c["score"] for c in q["cases"]] == sorted((c["score"] for c in q["cases"]), reverse=True)
    assert len(q["duplicate_groups"]) == 1
    for c in q["cases"]:
        d = service.case_detail(sb, tenant, c["id"])
        assert d["brief"]["next_action"] and d["drafts"] and d["timeline"]
    assert service.case_detail(sb, tenant, "demo-1")["similar"]
    assert service.case_detail(sb, tenant, "demo-6")["duplicates"]


def test_review_hides_until_new_activity(sb, tenant):
    from interpreter.monitor import service

    service.load_demo(sb, tenant)
    service.set_review(sb, tenant, "demo-1", "57c26330-0000-0000-0000-000000000000", reviewed=True)
    row = next(c for c in service.queue(sb, tenant)["cases"] if c["id"] == "demo-1")
    assert row["hidden"]


def test_sync_lock_allows_one_running(sb, tenant):
    from interpreter.monitor.store import SupabaseStore

    store = SupabaseStore(sb)
    first = store.start_sync(tenant, "test")
    assert first and store.start_sync(tenant, "test") is None
    store.finish_sync(first, "ok", {"my_turn": 1}, [])
    second = store.start_sync(tenant, "test")
    assert second
    store.finish_sync(second, "ok", {}, [])


def test_store_reads_run_against_real_schema(sb, tenant):
    from interpreter.monitor.store import SupabaseStore

    store = SupabaseStore(sb)
    assert isinstance(store.ai_feed(tenant), dict)
    assert isinstance(store.open_cases(tenant), dict)
    store.last_resolved_at(tenant)


def test_sync_without_an_org_fails_cleanly(sb, tenant):
    from interpreter.monitor import service

    result = service.run_sync(sb, tenant, "test")
    assert result["status"] == "failed" and "Connect the workspace" in result["error"]
    assert service.queue(sb, tenant)["last_sync"]["status"] == "failed"


def test_anon_key_cannot_read_monitor_tables(sb, tenant):
    import os
    from interpreter.monitor import service
    from interpreter.supabase_client import make_client

    key = os.environ.get("SUPABASE_ANON_KEY")
    if not key:
        pytest.skip("needs the anon key")
    service.load_demo(sb, tenant)
    anon = make_client(os.environ["SUPABASE_URL"], key)
    for table in ("monitor_cases", "monitor_evidence", "monitor_config", "monitor_syncs"):
        assert anon.table(table).select("*").limit(1).execute().data == []


def test_alerts_flow_through_real_tables(sb, tenant):
    from interpreter.monitor import service
    from interpreter.monitor.store import SupabaseStore

    user = "57c26330-0000-0000-0000-000000000001"
    sync_id = SupabaseStore(sb).start_sync(tenant, "test")
    SupabaseStore(sb).finish_sync(sync_id, "ok", {}, [])
    try:
        service.start_alerts(sb, tenant, user)
        SupabaseStore(sb).add_events(tenant, sync_id, [
            {"case_id": "alert-test", "case_number": "1", "subject": "S", "kind": "needs_action",
             "detail": "Fix shared"}])
        got = service.alerts(sb, tenant, user)
        assert got["unread"] == 1 and got["alerts"][0]["detail"] == "Fix shared"
        service.set_review(sb, tenant, "alert-test", user, reviewed=True)
        assert service.alerts(sb, tenant, user)["unread"] == 0
        service.set_review(sb, tenant, "alert-test", user, clear=True)
        assert service.alerts(sb, tenant, user)["unread"] == 1
        service.mark_alerts_seen(sb, tenant, user)
        assert service.alerts(sb, tenant, user)["unread"] == 0
    finally:
        sb.table("monitor_events").delete().eq("case_id", "alert-test").execute()
        sb.table("monitor_reviews").delete().eq("case_id", "alert-test").execute()
        sb.table("monitor_alert_seen").delete().eq("user_id", user).execute()
