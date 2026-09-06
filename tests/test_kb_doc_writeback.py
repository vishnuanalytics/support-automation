"""KB write-back to Google Docs (docs/KB_SOURCE_CONNECTORS.md §2).

Covers the paragraph-diff block extraction, the `apply_kb_change` gate
(fires `gdoc_writeback` only for a `write_back` gdocs connection), and the
`_gdoc_writeback` worker job's applied / partial / conflict / error paths.
Offline: `gdrive` and `github` are mocked, no Google/GitHub creds needed.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api import worker
from interpreter import kb_connectors, kb_writeback


# ── fake Supabase ─────────────────────────────────────────────────────────
class _Table:
    def __init__(self, rows):
        self.rows = rows
        self._f: dict = {}
        self._pending = None

    def select(self, *_a, **_k):
        return self

    def eq(self, k, v):
        self._f[k] = v
        return self

    def limit(self, _n):
        return self

    def insert(self, row):
        self._pending = ("insert", dict(row))
        return self

    def update(self, patch):
        self._pending = ("update", dict(patch))
        return self

    def execute(self):
        op, data = self._pending or (None, None)
        if op == "insert":
            new = {"id": f"w{len(self.rows) + 1}", **data}
            self.rows.append(new)
            self._pending, self._f = None, {}
            return type("R", (), {"data": [new]})()
        matches = [r for r in self.rows if all(r.get(k) == v for k, v in self._f.items())]
        if op == "update":
            for r in matches:
                r.update(data)
        self._pending, self._f = None, {}
        return type("R", (), {"data": matches})()


class _SB:
    def __init__(self, **tables):
        self._t = {name: _Table(rows) for name, rows in tables.items()}

    def table(self, name):
        return self._t.setdefault(name, _Table([]))


# ── _doc_change_blocks ───────────────────────────────────────────────────
def test_change_blocks_replace():
    blocks = kb_writeback._doc_change_blocks(
        "# Title\n\nRefunds under $200 auto-approve.\n\nContact billing for more.",
        "# Title\n\nRefunds under $500 auto-approve.\n\nContact billing for more.",
    )
    assert blocks == [{"old": "Refunds under $200 auto-approve.",
                       "new": "Refunds under $500 auto-approve."}]


def test_change_blocks_insert_has_empty_old():
    blocks = kb_writeback._doc_change_blocks("Para one.", "Para one.\n\nA brand new para.")
    assert blocks == [{"old": "", "new": "A brand new para."}]


def test_change_blocks_none_when_identical():
    assert kb_writeback._doc_change_blocks("same\n\ntext", "same\n\ntext") == []


# ── apply_kb_change gate: _maybe_enqueue_doc_writeback ────────────────────
def _entry_row(**over):
    r = {"entry_id": "old1", "body_md": "old para", "connection_id": "c1",
         "gdoc_modified": "2026-01-01T00:00:00Z"}
    r.update(over)
    return r


def _conn_row(**over):
    r = {"connection_id": "c1", "connector": "gdocs",
         "config": {"doc_id": "d1", "doc_url": "u", "access": "write_back",
                    "github_repo": "acme/kb"}}
    r.update(over)
    return r


def _enq_capture(monkeypatch):
    calls = []
    monkeypatch.setattr("interpreter.jobs.enqueue",
                        lambda kind, payload, **kw: calls.append((kind, payload)) or "j1")
    return calls


def test_writeback_enqueued_for_a_write_back_gdocs_connection(monkeypatch):
    sb = _SB(kb_entries=[_entry_row()], kb_source_connections=[_conn_row()])
    calls = _enq_capture(monkeypatch)
    kb_writeback._maybe_enqueue_doc_writeback(
        sb, old_id="old1", tenant_id="t", new_body_md="new para",
        approver="mgr", review_task_id="rt1")
    assert [k for k, _ in calls] == ["gdoc_writeback"]
    p = calls[0][1]
    assert p["connection_id"] == "c1" and p["github_repo"] == "acme/kb"
    assert p["blocks"] == [{"old": "old para", "new": "new para"}]
    assert p["old_modified"] == "2026-01-01T00:00:00Z"


def test_writeback_not_enqueued_when_read_only(monkeypatch):
    sb = _SB(kb_entries=[_entry_row()],
             kb_source_connections=[_conn_row(config={"doc_id": "d1", "access": "read_only"})])
    calls = _enq_capture(monkeypatch)
    kb_writeback._maybe_enqueue_doc_writeback(
        sb, old_id="old1", tenant_id="t", new_body_md="new", approver="m", review_task_id=None)
    assert calls == []


def test_writeback_not_enqueued_for_a_non_gdoc_connection(monkeypatch):
    sb = _SB(kb_entries=[_entry_row()],
             kb_source_connections=[_conn_row(connector="public_url")])
    calls = _enq_capture(monkeypatch)
    kb_writeback._maybe_enqueue_doc_writeback(
        sb, old_id="old1", tenant_id="t", new_body_md="new", approver="m", review_task_id=None)
    assert calls == []


def test_writeback_not_enqueued_when_entry_has_no_connection(monkeypatch):
    sb = _SB(kb_entries=[_entry_row(connection_id=None)], kb_source_connections=[])
    calls = _enq_capture(monkeypatch)
    kb_writeback._maybe_enqueue_doc_writeback(
        sb, old_id="old1", tenant_id="t", new_body_md="new", approver="m", review_task_id=None)
    assert calls == []


# ── worker._gdoc_writeback ───────────────────────────────────────────────
def _wire_gdrive_github(monkeypatch, *, modified="M1", replace_returns=1, fetch_raises=None):
    def fetch_doc(tid, did, sb):
        if fetch_raises:
            raise fetch_raises
        return {"title": "Billing SOP", "markdown": "# Billing SOP\n\nbody", "modified_time": modified}

    monkeypatch.setattr("interpreter.gdrive.fetch_doc", fetch_doc)
    monkeypatch.setattr("interpreter.gdrive.replace_passage",
                        lambda tid, did, old, new, sb: replace_returns)
    commented = []
    monkeypatch.setattr("interpreter.gdrive.comment",
                        lambda tid, did, text, sb: commented.append(text) or "cmt1")
    monkeypatch.setattr("interpreter.github.token_for", lambda tid, sb: "tok")
    issues = []
    monkeypatch.setattr("interpreter.github.create_issue",
                        lambda tok, repo, **kw: issues.append((repo, kw)) or
                        {"html_url": f"https://github.com/{repo}/issues/7", "number": 7})
    enq = []
    monkeypatch.setattr("interpreter.jobs.enqueue",
                        lambda kind, payload, **kw: enq.append((kind, payload)) or "j1")
    return {"commented": commented, "issues": issues, "enq": enq}


def _payload(**over):
    p = {"connection_id": "c1", "tenant_id": "t", "entry_id": "old1", "approver": "mgr",
         "review_task_id": "rt1", "github_repo": "acme/kb", "old_modified": "M1",
         "blocks": [{"old": "under $200", "new": "under $500"}]}
    p.update(over)
    return p


def test_gdoc_writeback_applied(monkeypatch):
    sb = _SB(kb_source_connections=[_conn_row()])
    seen = _wire_gdrive_github(monkeypatch, replace_returns=1)
    out = worker._gdoc_writeback(_payload(), sb)

    assert out["status"] == "applied"
    row = sb.table("kb_doc_writebacks").rows[0]
    assert row["status"] == "applied"
    assert row["blocks"] == [{"old": "under $200", "new": "under $500", "applied": True}]
    assert row["github_issue_url"].endswith("/issues/7")
    assert row["pre_edit_markdown"] == "# Billing SOP\n\nbody"
    assert seen["issues"] and seen["issues"][0][0] == "acme/kb"
    assert seen["commented"]
    assert ("kb_sync", {"connection_id": "c1"}) in seen["enq"]


def test_gdoc_writeback_partial_when_a_block_does_not_match(monkeypatch):
    sb = _SB(kb_source_connections=[_conn_row()])
    _wire_gdrive_github(monkeypatch, replace_returns=0)
    out = worker._gdoc_writeback(_payload(), sb)
    assert out["status"] == "partial" or out["status"] == "conflict"
    # 0 replacements on the only block -> nothing applied -> "conflict"
    assert out["status"] == "conflict"
    assert sb.table("kb_doc_writebacks").rows[0]["blocks"][0]["applied"] is False


def test_gdoc_writeback_conflict_when_doc_moved(monkeypatch):
    sb = _SB(kb_source_connections=[_conn_row()])
    calls = {"replace": 0}
    seen = _wire_gdrive_github(monkeypatch, modified="M2")  # != payload old_modified "M1"
    monkeypatch.setattr("interpreter.gdrive.replace_passage",
                        lambda *a, **k: calls.__setitem__("replace", calls["replace"] + 1) or 1)
    out = worker._gdoc_writeback(_payload(), sb)
    assert out["status"] == "conflict"
    assert calls["replace"] == 0                       # never edited the doc
    assert ("kb_sync", {"connection_id": "c1"}) not in seen["enq"]
    assert sb.table("kb_doc_writebacks").rows[0]["status"] == "conflict"


def test_gdoc_writeback_records_a_fetch_error(monkeypatch):
    sb = _SB(kb_source_connections=[_conn_row()])
    _wire_gdrive_github(monkeypatch, fetch_raises=RuntimeError("token expired"))
    out = worker._gdoc_writeback(_payload(), sb)
    assert "error" in out
    row = sb.table("kb_doc_writebacks").rows[0]
    assert row["status"] == "error" and "token expired" in row["error"]


# ── gdocs connector: access / github_repo normalize + writable flag ──────
def test_gdocs_is_writable():
    assert kb_connectors.get_kb_connector("gdocs").writable is True
    assert kb_connectors.get_kb_connector("public_url").writable is False


def test_gdocs_normalize_defaults_to_read_only():
    cfg = kb_connectors.get_kb_connector("gdocs").normalize_config(
        {"doc_url": "https://docs.google.com/document/d/ABCdef123456789012345/edit"})
    assert cfg["access"] == "read_only" and "github_repo" not in cfg


def test_gdocs_normalize_write_back_requires_a_valid_repo():
    spec = kb_connectors.get_kb_connector("gdocs")
    url = "https://docs.google.com/document/d/ABCdef123456789012345/edit"
    cfg = spec.normalize_config({"doc_url": url, "access": "write_back",
                                 "github_repo": "acme/support-kb"})
    assert cfg == {"doc_id": "ABCdef123456789012345", "doc_url": url,
                   "access": "write_back", "github_repo": "acme/support-kb"}
    with pytest.raises(ValueError):
        spec.normalize_config({"doc_url": url, "access": "write_back", "github_repo": "not-a-repo"})
