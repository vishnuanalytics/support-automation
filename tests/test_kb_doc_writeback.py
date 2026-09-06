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

    def in_(self, k, vals):
        self._f[("in", k)] = list(vals)
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
        def _ok(r):
            for k, v in self._f.items():
                if isinstance(k, tuple) and k[0] == "in":
                    if r.get(k[1]) not in v:
                        return False
                elif r.get(k) != v:
                    return False
            return True

        matches = [r for r in self.rows if _ok(r)]
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


def test_writeback_enqueued_for_a_suggest_connection_with_mode_in_payload(monkeypatch):
    sb = _SB(kb_entries=[_entry_row()],
             kb_source_connections=[_conn_row(config={"doc_id": "d1", "access": "suggest",
                                                      "github_repo": "acme/kb"})])
    calls = _enq_capture(monkeypatch)
    kb_writeback._maybe_enqueue_doc_writeback(
        sb, old_id="old1", tenant_id="t", new_body_md="new para",
        approver="mgr", review_task_id="rt1")
    assert [k for k, _ in calls] == ["gdoc_writeback"]
    assert calls[0][1]["mode"] == "suggest"


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
    replaced = []
    monkeypatch.setattr("interpreter.gdrive.replace_passage",
                        lambda tid, did, old, new, sb: replaced.append((old, new)) or replace_returns)
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
    return {"commented": commented, "issues": issues, "enq": enq, "replaced": replaced}


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


def test_gdoc_writeback_suggest_opens_an_issue_but_never_edits_the_doc(monkeypatch):
    sb = _SB(kb_source_connections=[_conn_row(config={"doc_id": "d1", "doc_url": "u",
                                                     "access": "suggest",
                                                     "github_repo": "acme/kb"})])
    seen = _wire_gdrive_github(monkeypatch, replace_returns=1)
    out = worker._gdoc_writeback(_payload(mode="suggest"), sb)

    assert out["status"] == "suggested" and out["mode"] == "suggest"
    assert seen["replaced"] == []                      # the doc was NOT touched
    row = sb.table("kb_doc_writebacks").rows[0]
    assert row["status"] == "suggested"
    assert row["blocks"][0]["applied"] is False
    assert row["github_issue_url"].endswith("/issues/7")
    assert seen["issues"] and "correction to apply" in seen["issues"][0][1]["title"]
    assert seen["commented"] and "proposed" in seen["commented"][0]
    assert ("kb_sync", {"connection_id": "c1"}) not in seen["enq"]   # nothing changed yet


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


def test_gdocs_access_field_has_human_labels_and_help_for_the_ui():
    spec = kb_connectors.get_kb_connector("gdocs")
    access = next(f for f in spec.config_fields if f["key"] == "access")
    assert set(access["options"]) == {"read_only", "suggest", "write_back"}
    # every option carries a plain-language label + the field has help text
    assert set(access["option_labels"]) == set(access["options"])
    assert "recommended" in access["option_labels"]["suggest"]
    assert access.get("help")
    repo = next(f for f in spec.config_fields if f["key"] == "github_repo")
    assert repo["show_if"] == {"key": "access", "ne": "read_only"} and repo.get("help")


def test_gdocs_normalize_suggest_also_needs_a_repo():
    spec = kb_connectors.get_kb_connector("gdocs")
    url = "https://docs.google.com/document/d/ABCdef123456789012345/edit"
    cfg = spec.normalize_config({"doc_url": url, "access": "suggest",
                                 "github_repo": "acme/kb"})
    assert cfg["access"] == "suggest" and cfg["github_repo"] == "acme/kb"
    with pytest.raises(ValueError):
        spec.normalize_config({"doc_url": url, "access": "suggest"})
    with pytest.raises(ValueError):
        spec.normalize_config({"doc_url": url, "access": "bogus"})


# ── watch_doc_writebacks (chunk 2: close the loop) ───────────────────────
def _wb_row(**over):
    r = {"id": "wb1", "tenant_id": "t", "connection_id": "c1", "status": "applied",
         "github_repo": "acme/kb", "github_issue_number": 5,
         "blocks": [{"old": "was A", "new": "now B", "applied": True}]}
    r.update(over)
    return r


def _wire_watch(monkeypatch, *, state="open", comments=None, get_raises=None):
    monkeypatch.setattr("interpreter.github.token_for", lambda t, s: "tok")

    def get_issue(tok, repo, num):
        if get_raises:
            raise get_raises
        return {"number": num, "state": state, "html_url": f"https://github.com/{repo}/issues/{num}",
                "title": "t", "labels": ["kb-writeback"]}

    monkeypatch.setattr("interpreter.github.get_issue", get_issue)
    monkeypatch.setattr("interpreter.github.list_issue_comments",
                        lambda tok, repo, num: comments or [])
    posted = []
    monkeypatch.setattr("interpreter.github.add_issue_comment",
                        lambda tok, repo, num, body: posted.append(body) or {"id": 1, "html_url": ""})
    reversed_blocks = []
    monkeypatch.setattr("interpreter.gdrive.replace_passage",
                        lambda t, d, old, new, s: reversed_blocks.append((old, new)) or 1)
    enq = []
    monkeypatch.setattr("interpreter.jobs.enqueue",
                        lambda kind, payload, **kw: enq.append((kind, payload)) or "j1")
    return {"posted": posted, "reversed": reversed_blocks, "enq": enq}


def test_watch_marks_verified_when_the_issue_is_closed(monkeypatch):
    sb = _SB(kb_doc_writebacks=[_wb_row()])
    _wire_watch(monkeypatch, state="closed")
    res = kb_writeback.watch_doc_writebacks(sb)
    assert res == {"checked": 1, "verified": 1, "reverted": 0, "errors": 0, "dry_run": False}
    row = sb.table("kb_doc_writebacks").rows[0]
    assert row["status"] == "verified" and row["verified_at"] == "now()"


def test_watch_reverts_on_a_slash_revert_comment(monkeypatch):
    sb = _SB(kb_doc_writebacks=[_wb_row()],
             kb_source_connections=[{"connection_id": "c1", "config": {"doc_id": "d1"}}])
    seen = _wire_watch(monkeypatch, state="open",
                       comments=[{"body": "looks wrong", "user": "u"},
                                 {"body": "/revert please", "user": "mgr"}])
    res = kb_writeback.watch_doc_writebacks(sb)
    assert res["reverted"] == 1 and res["verified"] == 0
    assert sb.table("kb_doc_writebacks").rows[0]["status"] == "reverted"
    assert seen["reversed"] == [("now B", "was A")]          # new -> old, in the doc
    assert ("kb_sync", {"connection_id": "c1"}) in seen["enq"]
    assert seen["posted"] and "Reverted" in seen["posted"][0]


def test_watch_revert_takes_precedence_over_a_close(monkeypatch):
    sb = _SB(kb_doc_writebacks=[_wb_row()],
             kb_source_connections=[{"connection_id": "c1", "config": {"doc_id": "d1"}}])
    _wire_watch(monkeypatch, state="closed", comments=[{"body": "/revert", "user": "m"}])
    res = kb_writeback.watch_doc_writebacks(sb)
    assert res["reverted"] == 1 and res["verified"] == 0


def test_watch_ignores_revert_on_a_conflict_row(monkeypatch):
    sb = _SB(kb_doc_writebacks=[_wb_row(status="conflict", blocks=[{"old": "x", "new": "y",
                                                                   "applied": False}])])
    _wire_watch(monkeypatch, state="closed", comments=[{"body": "/revert", "user": "m"}])
    res = kb_writeback.watch_doc_writebacks(sb)
    assert res["reverted"] == 0 and res["verified"] == 1     # nothing to revert; close = handled


def test_watch_dry_run_writes_nothing(monkeypatch):
    sb = _SB(kb_doc_writebacks=[_wb_row()])
    _wire_watch(monkeypatch, state="closed")
    res = kb_writeback.watch_doc_writebacks(sb, dry_run=True)
    assert res["verified"] == 1 and res["dry_run"] is True
    assert sb.table("kb_doc_writebacks").rows[0]["status"] == "applied"   # untouched


def test_watch_skips_rows_without_an_issue_and_counts_api_errors(monkeypatch):
    sb = _SB(kb_doc_writebacks=[
        _wb_row(id="wb1", github_issue_number=None),
        _wb_row(id="wb2", github_issue_number=9),
    ])
    _wire_watch(monkeypatch, get_raises=RuntimeError("gh 404"))
    res = kb_writeback.watch_doc_writebacks(sb)
    assert res["checked"] == 1 and res["errors"] == 1        # wb1 skipped, wb2 errored
    assert all(r["status"] == "applied" for r in sb.table("kb_doc_writebacks").rows)


def test_watch_only_looks_at_open_writeback_statuses(monkeypatch):
    sb = _SB(kb_doc_writebacks=[
        _wb_row(id="done", status="verified"),
        _wb_row(id="gone", status="reverted"),
    ])
    _wire_watch(monkeypatch, state="closed")
    res = kb_writeback.watch_doc_writebacks(sb)
    assert res["checked"] == 0


def test_watch_suggested_row_closed_verifies_and_resyncs(monkeypatch):
    sb = _SB(kb_doc_writebacks=[_wb_row(status="suggested", connection_id="c9")])
    seen = _wire_watch(monkeypatch, state="closed")
    res = kb_writeback.watch_doc_writebacks(sb)
    assert res["verified"] == 1 and res["reverted"] == 0
    assert sb.table("kb_doc_writebacks").rows[0]["status"] == "verified"
    # a suggest-mode close means a human edited the doc -> re-sync the mirror now
    assert ("kb_sync", {"connection_id": "c9"}) in seen["enq"]


def test_watch_suggested_row_still_open_is_untouched(monkeypatch):
    sb = _SB(kb_doc_writebacks=[_wb_row(status="suggested", connection_id="c9")])
    seen = _wire_watch(monkeypatch, state="open")
    res = kb_writeback.watch_doc_writebacks(sb)
    assert res["verified"] == 0
    assert sb.table("kb_doc_writebacks").rows[0]["status"] == "suggested"
    assert seen["enq"] == []


def test_watch_revert_survives_a_row_with_no_connection(monkeypatch):
    # a kb_doc_writebacks row can have connection_id=None; _do_doc_revert must
    # not build a `connection_id=eq.None` query (live-caught regression).
    sb = _SB(kb_doc_writebacks=[_wb_row(connection_id=None)])
    _wire_watch(monkeypatch, state="open", comments=[{"body": "/revert", "user": "m"}])
    res = kb_writeback.watch_doc_writebacks(sb)
    assert res["reverted"] == 1
    assert sb.table("kb_doc_writebacks").rows[0]["status"] == "reverted"
