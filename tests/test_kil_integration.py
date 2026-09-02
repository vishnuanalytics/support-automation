"""P2b — KIL write-path integration tests against the real DB.

These exercise the code paths that the offline suite can't: PostgREST
`on_conflict` behaviour and every column the KIL writers touch. Three of the
four bugs the KIL live-smoke found would have failed here. Skipped without
SUPABASE_URL + SUPABASE_SERVICE_KEY.
"""

from __future__ import annotations

import os
import pathlib
import sys
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY")),
        reason="no SUPABASE creds"),
]

TEN = os.environ.get("DEFAULT_TENANT_ID", "00000000-0000-0000-0000-000000000000")


@pytest.fixture
def sb():
    from ingestion.scraper import get_supabase
    return get_supabase()


def test_review_open_task_is_idempotent_on_run_kind(sb):
    """The partial-index-vs-PostgREST-upsert bug: a second flag for the same
    (run_id, kind) must be a no-op, not a 42P10."""
    from interpreter import review
    run_id = str(uuid.uuid4())
    try:
        a = review.open_task(sb, tenant_id=TEN, run_id=run_id, kind="human_reply_review",
                             trigger="contradicts", statement="x contradicts y",
                             verdict={"relation": "contradicts", "flagged": True}, contexts=[])
        assert a and a["id"]
        b = review.open_task(sb, tenant_id=TEN, run_id=run_id, kind="human_reply_review",
                             trigger="contradicts", statement="again",
                             verdict={}, contexts=[])
        assert b is None or b.get("id") == a["id"]      # deduped, no exception
        rows = (sb.table("review_tasks").select("id")
                .eq("run_id", run_id).eq("kind", "human_reply_review").execute().data or [])
        assert len(rows) == 1
    finally:
        sb.table("review_tasks").delete().eq("run_id", run_id).execute()


def test_kb_writeback_apply_touches_only_real_columns(sb):
    """`_corrections_source` + the provisional insert must use columns that
    actually exist on `sources` / `kb_entries` (the display_name bug)."""
    from interpreter import kb_writeback
    ar = {
        "id": str(uuid.uuid4()), "tenant_id": TEN, "kind": "kb_change",
        "status": "approved", "decided_by": "ci", "result": None,
        "payload": {"op": "create", "title": "CI provisional entry",
                    "body_md": "CI test body — safe to delete.",
                    "review_task_id": None},
    }
    entry_id = None
    try:
        res = kb_writeback.apply_kb_change(sb, ar, enqueue=False)
        entry_id = res["entry_id"]
        row = sb.table("kb_entries").select("*").eq("entry_id", entry_id).execute().data[0]
        assert row["status"] == "provisional"
        assert row["origin"] == "review_writeback"
        assert row["provisional_until"]
    finally:
        if entry_id:
            sb.table("doc_chunks").delete().eq("doc_url", f"kb://%/{entry_id}").execute()
            sb.table("kb_entries").delete().eq("entry_id", entry_id).execute()
        sb.table("sources").delete().eq("tenant_id", TEN).eq("name", "kb-corrections").execute()


def test_introspect_schema_rpc_is_callable(sb):
    d = sb.rpc("introspect_schema").execute().data
    assert isinstance(d, dict) and "review_tasks" in d["tables"]
    assert {"table": "doc_chunks", "column": "entry_status"} in d["columns"]
