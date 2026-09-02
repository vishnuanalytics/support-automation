"""P2a — the migration/schema drift checker (offline: parse + diff only)."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.verify_migrations import diff, parse_expectations


def test_parse_expectations_covers_the_repo_migrations():
    e = parse_expectations()
    # a spread of real objects across the migration history
    assert "review_tasks" in e["tables"]
    assert "doc_chunks" in e["tables"]
    assert ("doc_chunks", "entry_status") in e["columns"]       # migration 068 ALTER
    assert ("review_tasks", "kb_change_id") in e["columns"]     # migration 065 CREATE TABLE
    assert "introspect_schema" in e["functions"]                # migration 070
    assert "uq_review_tasks_run_kind" in e["indexes"]
    # constraint lines in a CREATE TABLE body are not mistaken for columns
    assert ("review_tasks", "constraint") not in e["columns"]
    assert ("review_tasks", "primary") not in e["columns"]


def test_diff_flags_a_missing_column_but_not_one_on_an_absent_table():
    expected = {
        "tables": {"a", "b"},
        "columns": {("a", "x"), ("a", "y"), ("b", "z")},
        "functions": {"f"}, "indexes": {"i"},
    }
    live = {
        "tables": {"a", "supabase_migrations"},   # `b` never applied
        "columns": {("a", "x")},                  # a.y missing
        "functions": set(), "indexes": set(),
    }
    d = diff(expected, live)
    assert d["missing_tables"] == ["b"]
    assert d["missing_columns"] == ["a.y"]        # not "b.z" — b isn't live
    assert d["missing_functions"] == ["f"]
    assert d["missing_indexes"] == ["i"]
    assert d["unexpected_tables"] == []           # supabase_ prefix filtered


def test_diff_notes_a_hand_created_table():
    d = diff(
        {"tables": {"a"}, "columns": set(), "functions": set(), "indexes": set()},
        {"tables": {"a", "snuck_in"}, "columns": set(), "functions": set(), "indexes": set()},
    )
    assert d["unexpected_tables"] == ["snuck_in"]
    assert not d["missing_tables"]


def test_the_live_repo_schema_has_no_drift():
    """If SUPABASE creds are present this runs for real; otherwise it's a
    no-op. Kept unmarked (not `integration`) so it's a cheap local guard."""
    from scripts.verify_migrations import live_schema
    live = live_schema()
    if live is None:
        return
    d = diff(parse_expectations(), live)
    assert not d["missing_tables"], d["missing_tables"]
    assert not d["missing_columns"], d["missing_columns"]
    assert not d["missing_functions"], d["missing_functions"]
