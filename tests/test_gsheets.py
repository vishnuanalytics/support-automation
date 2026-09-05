"""Google Sheets KB connector (docs/KB_SOURCE_CONNECTORS.md) — one KB
document per data row, not the whole sheet as one blob. Offline: the
Drive/Sheets API clients are mocked, no real Google creds needed."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import gsheets


def test_parse_sheet_id_from_url():
    assert gsheets.parse_sheet_id(
        "https://docs.google.com/spreadsheets/d/1AbCdEf_ghIJK/edit#gid=0") == "1AbCdEf_ghIJK"


def test_parse_sheet_id_from_bare_id():
    assert gsheets.parse_sheet_id("1AbCdEf_ghIJKlmno-pQRstuvWXyz012345") \
        == "1AbCdEf_ghIJKlmno-pQRstuvWXyz012345"


def test_parse_sheet_id_rejects_garbage():
    with pytest.raises(ValueError):
        gsheets.parse_sheet_id("https://example.com/not-a-sheet")


def test_row_doc_skips_a_fully_empty_row():
    assert gsheets._row_doc(["Question", "Answer"], ["", ""]) is None
    assert gsheets._row_doc(["Question", "Answer"], []) is None


def test_row_doc_titles_from_first_non_empty_cell_and_lists_all_columns():
    title, body = gsheets._row_doc(
        ["Question", "Answer", "Category"],
        ["How do I reset my password?", "Use the Forgot Password link.", ""],
    )
    assert title == "How do I reset my password?"
    assert body == ("**Question:** How do I reset my password?\n"
                    "**Answer:** Use the Forgot Password link.")
    assert "Category" not in body   # empty cell dropped, not rendered as blank


def test_row_doc_takes_the_first_non_empty_cell_even_if_not_the_first_column():
    title, body = gsheets._row_doc(["Category", "Question"], ["", "What plans exist?"])
    assert title == "What plans exist?"


class _Exec:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return self._data


class _FakeFiles:
    def __init__(self, meta):
        self._meta = meta

    def get(self, fileId, fields):  # noqa: N803 -- matches the real google client's kwarg name
        assert fileId == "sheet123"
        return _Exec(self._meta)


class _FakeDrive:
    def __init__(self, meta):
        self._files = _FakeFiles(meta)

    def files(self):
        return self._files


class _FakeValues:
    def __init__(self, values):
        self._values = values

    def get(self, spreadsheetId, range):  # noqa: N803, A002
        assert spreadsheetId == "sheet123"
        self.last_range = range
        return _Exec({"values": self._values})


class _FakeSpreadsheetsMeta:
    def __init__(self, first_tab_title):
        self._title = first_tab_title

    def get(self, spreadsheetId, fields):  # noqa: N803
        return _Exec({"sheets": [{"properties": {"title": self._title}}]})


class _FakeSpreadsheets:
    def __init__(self, values, first_tab_title):
        self._values_obj = _FakeValues(values)
        self._meta = _FakeSpreadsheetsMeta(first_tab_title)

    def values(self):
        return self._values_obj

    def get(self, spreadsheetId, fields):  # noqa: N803
        return self._meta.get(spreadsheetId, fields)


class _FakeSheets:
    def __init__(self, values, first_tab_title="Sheet1"):
        self._spreadsheets = _FakeSpreadsheets(values, first_tab_title)

    def spreadsheets(self):
        return self._spreadsheets


def _mock_services(monkeypatch, *, meta, values, first_tab_title="Sheet1"):
    drive = _FakeDrive(meta)
    sheets = _FakeSheets(values, first_tab_title)
    monkeypatch.setattr(gsheets, "_services", lambda tid, sb: (drive, sheets))
    return drive, sheets


def test_fetch_sheet_rejects_the_wrong_mimetype(monkeypatch):
    _mock_services(monkeypatch, meta={"name": "Not a sheet", "mimeType": "application/pdf",
                                      "modifiedTime": "t"}, values=[])
    with pytest.raises(ValueError):
        gsheets.fetch_sheet("t1", "sheet123", sb=None)


def test_fetch_sheet_uses_the_first_tab_when_no_sheet_name_given(monkeypatch):
    _, sheets = _mock_services(
        monkeypatch,
        meta={"name": "FAQ", "mimeType": "application/vnd.google-apps.spreadsheet",
              "modifiedTime": "2026-09-05T00:00:00Z"},
        values=[["Question", "Answer"], ["Q1", "A1"], ["", ""], ["Q2", "A2"]],
        first_tab_title="Support FAQ",
    )
    out = gsheets.fetch_sheet("t1", "sheet123", sb=None)
    assert out["tab"] == "Support FAQ"
    assert out["title"] == "FAQ"
    assert out["modified_time"] == "2026-09-05T00:00:00Z"
    # header is row 1, empty row 3 skipped, data rows numbered from row 2
    assert [r["row"] for r in out["rows"]] == [2, 4]
    assert out["rows"][0]["title"] == "Q1"


def test_fetch_sheet_honors_an_explicit_sheet_name(monkeypatch):
    drive, sheets = _mock_services(
        monkeypatch,
        meta={"name": "FAQ", "mimeType": "application/vnd.google-apps.spreadsheet",
              "modifiedTime": "t"},
        values=[["Question", "Answer"], ["Q1", "A1"]],
    )
    out = gsheets.fetch_sheet("t1", "sheet123", sheet_name="Archive", sb=None)
    assert out["tab"] == "Archive"
    assert sheets.spreadsheets().values().last_range == "Archive"


def test_fetch_sheet_empty_sheet_yields_no_rows(monkeypatch):
    _mock_services(monkeypatch, meta={"name": "Empty", "mimeType": "application/vnd.google-apps.spreadsheet",
                                      "modifiedTime": "t"}, values=[])
    out = gsheets.fetch_sheet("t1", "sheet123", sb=None)
    assert out["rows"] == []


# ── api/worker.py::_sync_gsheet ──────────────────────────────────────────
class _Table:
    def __init__(self, rows):
        self.rows = rows
        self._filters: dict = {}
        self._pending: tuple[str, dict] | None = None

    def select(self, *_a, **_k):
        return self

    def eq(self, k, v):
        self._filters[k] = v
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
            new_row = {"entry_id": f"e{len(self.rows) + 1}", **data}
            self.rows.append(new_row)
            self._pending = None
            self._filters = {}
            return type("R", (), {"data": [new_row]})()
        matches = [r for r in self.rows if all(r.get(k) == v for k, v in self._filters.items())]
        if op == "update":
            for r in matches:
                r.update(data)
            self._pending = None
        self._filters = {}
        return type("R", (), {"data": matches})()


class _SB:
    def __init__(self, sources_rows=None, kb_rows=None):
        self._t = {"sources": _Table(sources_rows or []), "kb_entries": _Table(kb_rows or [])}

    def table(self, name):
        return self._t[name]


def test_worker_sync_gsheet_creates_one_entry_per_row(monkeypatch):
    from api import worker

    monkeypatch.setattr(gsheets, "fetch_sheet", lambda tid, sid, *, sheet_name, sb: {
        "title": "FAQ", "modified_time": "t", "tab": "Sheet1",
        "rows": [{"row": 2, "title": "Q1", "body_md": "**Q:** Q1\n**A:** A1"},
                {"row": 3, "title": "Q2", "body_md": "**Q:** Q2\n**A:** A2"}],
    })
    enq = []
    monkeypatch.setattr("interpreter.jobs.enqueue",
                        lambda kind, payload, **kw: enq.append((kind, payload["entry_id"])))

    sb = _SB(sources_rows=[{"source_id": "s", "config": {}}])
    out = worker._sync_gsheet({"source_id": "s", "tenant_id": "t", "collection_name": "c",
                               "sheet_id": "sheet123"}, sb)
    assert out["entries"] == 2
    assert [k for k, _ in enq] == ["embed_kb_entry", "embed_kb_entry"]
    assert sb.table("sources").rows[0]["config"]["gsheets"] == [
        {"sheet_id": "sheet123", "sheet_name": None}]


def test_worker_sync_gsheet_skips_an_unchanged_row(monkeypatch):
    from api import worker

    monkeypatch.setattr(gsheets, "fetch_sheet", lambda tid, sid, *, sheet_name, sb: {
        "title": "FAQ", "modified_time": "t", "tab": "Sheet1",
        "rows": [{"row": 2, "title": "Q1", "body_md": "same body"},
                {"row": 3, "title": "Q2", "body_md": "new body"}],
    })
    enq = []
    monkeypatch.setattr("interpreter.jobs.enqueue",
                        lambda kind, payload, **kw: enq.append(payload["entry_id"]))

    sb = _SB(
        sources_rows=[{"source_id": "s", "config": {}}],
        kb_rows=[{"entry_id": "e_old", "gsheet_row": 2, "body_md": "same body",
                 "source_id": "s", "origin": "gsheet", "gsheet_id": "sheet123", "status": "active"}],
    )
    out = worker._sync_gsheet({"source_id": "s", "tenant_id": "t", "collection_name": "c",
                               "sheet_id": "sheet123"}, sb)
    assert out["unchanged"] == 1
    assert out["entries"] == 1
    assert len(enq) == 1


def test_worker_sync_gsheet_archives_a_row_that_vanished(monkeypatch):
    from api import worker

    # sheet now only has row 2 -- row 3 (e_gone) disappeared. always safe to
    # archive here since fetch_sheet always reads the whole current range.
    monkeypatch.setattr(gsheets, "fetch_sheet", lambda tid, sid, *, sheet_name, sb: {
        "title": "FAQ", "modified_time": "t", "tab": "Sheet1",
        "rows": [{"row": 2, "title": "Q1", "body_md": "b1"}],
    })
    monkeypatch.setattr("interpreter.jobs.enqueue", lambda *a, **k: "job1")

    sb = _SB(
        sources_rows=[{"source_id": "s", "config": {}}],
        kb_rows=[
            {"entry_id": "e_keep", "gsheet_row": 2, "body_md": "old b1",
             "source_id": "s", "origin": "gsheet", "gsheet_id": "sheet123", "status": "active"},
            {"entry_id": "e_gone", "gsheet_row": 3, "body_md": "b2",
             "source_id": "s", "origin": "gsheet", "gsheet_id": "sheet123", "status": "active"},
        ],
    )
    out = worker._sync_gsheet({"source_id": "s", "tenant_id": "t", "collection_name": "c",
                               "sheet_id": "sheet123"}, sb)
    assert out["archived"] == 1
    gone = next(r for r in sb.table("kb_entries").rows if r["entry_id"] == "e_gone")
    assert gone["status"] == "archived"
    kept = next(r for r in sb.table("kb_entries").rows if r["entry_id"] == "e_keep")
    assert kept["status"] == "active"


def test_worker_sync_gsheet_dedups_config_across_reruns(monkeypatch):
    from api import worker

    monkeypatch.setattr(gsheets, "fetch_sheet", lambda tid, sid, *, sheet_name, sb: {
        "title": "FAQ", "modified_time": "t", "tab": "Sheet1", "rows": [],
    })
    sb = _SB(sources_rows=[{"source_id": "s",
                           "config": {"gsheets": [{"sheet_id": "sheet123", "sheet_name": None}]}}])
    worker._sync_gsheet({"source_id": "s", "tenant_id": "t", "collection_name": "c",
                        "sheet_id": "sheet123"}, sb)
    assert sb.table("sources").rows[0]["config"]["gsheets"] == [
        {"sheet_id": "sheet123", "sheet_name": None}]  # not duplicated


def test_worker_sync_gsheet_reports_a_fetch_error_without_raising(monkeypatch):
    from api import worker

    def boom(tid, sid, *, sheet_name, sb):
        raise RuntimeError("not connected")

    monkeypatch.setattr(gsheets, "fetch_sheet", boom)
    out = worker._sync_gsheet({"source_id": "s", "tenant_id": "t", "collection_name": "c",
                               "sheet_id": "sheet123"}, _SB())
    assert "error" in out
