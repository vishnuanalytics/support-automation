"""Phase 28 step 6 — bulk KB export/import (pure shaping/validation only)."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import kb_backup


def test_export_bundle_shapes_collection_and_entries():
    col = {"name": "Runbooks", "config": {"description": "internal SOPs"}}
    entries = [
        {"title": "A", "body_md": "hello", "status": "active"},
        {"title": "B", "body_md": "world", "status": "provisional"},
    ]
    bundle = kb_backup.export_bundle(col, entries)
    assert bundle["collection"] == {"name": "Runbooks", "description": "internal SOPs"}
    assert bundle["entries"] == [
        {"title": "A", "body_md": "hello", "status": "active"},
        {"title": "B", "body_md": "world", "status": "provisional"},
    ]


def test_export_bundle_tolerates_missing_description():
    bundle = kb_backup.export_bundle({"name": "X"}, [])
    assert bundle["collection"] == {"name": "X", "description": None}
    assert bundle["entries"] == []


def test_normalize_import_entries_happy_path():
    clean, warnings = kb_backup.normalize_import_entries([
        {"title": "A", "body_md": "hi"},
        {"title": "B", "body_md": "there"},
    ])
    assert clean == [{"title": "A", "body_md": "hi"}, {"title": "B", "body_md": "there"}]
    assert warnings == []


def test_normalize_import_entries_drops_bad_ones_with_a_warning():
    clean, warnings = kb_backup.normalize_import_entries([
        {"title": "A", "body_md": "hi"},
        "not an object",
        {"title": "", "body_md": "no title"},
        {"title": "no body"},
        {"title": "A", "body_md": "duplicate title"},
    ])
    assert clean == [{"title": "A", "body_md": "hi"}]
    assert len(warnings) == 4


def test_normalize_import_entries_not_a_list_is_a_clean_failure():
    clean, warnings = kb_backup.normalize_import_entries({"oops": "a dict, not a list"})
    assert clean == []
    assert warnings == ["entries must be a list"]


def test_normalize_import_entries_caps_at_max():
    raw = [{"title": f"t{i}", "body_md": "x"} for i in range(kb_backup.MAX_IMPORT_ENTRIES + 10)]
    clean, warnings = kb_backup.normalize_import_entries(raw)
    assert len(clean) == kb_backup.MAX_IMPORT_ENTRIES
    assert any("dropped" in w for w in warnings)


def test_normalize_import_entries_strips_whitespace_and_truncates_long_titles():
    clean, warnings = kb_backup.normalize_import_entries([
        {"title": "  padded  ", "body_md": "  body  "},
        {"title": "x" * 600, "body_md": "y"},
    ])
    assert clean[0] == {"title": "padded", "body_md": "body"}
    assert len(clean[1]["title"]) == 500
    assert warnings == []
