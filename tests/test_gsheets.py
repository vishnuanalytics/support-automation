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


# ── interpreter/kb_connectors: the gsheets connector's sync() ────────────
# The worker-side diff/upsert/embed/archive dance moved to the generic
# _sync_kb_connection driver (tests/test_kb_connectors.py).
from interpreter import kb_connectors  # noqa: E402


def _ctx():
    return kb_connectors.SyncCtx(tenant_id="t1", sb=None, collection_name="c")


def test_gsheets_sync_makes_one_document_per_row(monkeypatch):
    monkeypatch.setattr(gsheets, "fetch_sheet", lambda tid, sid, *, sheet_name, sb: {
        "title": "FAQ", "modified_time": "2026-01-01T00:00:00Z", "tab": "Sheet1",
        "rows": [{"row": 2, "title": "Q1", "body_md": "**Q:** Q1"},
                 {"row": 3, "title": "Q2", "body_md": "**Q:** Q2"}],
    })
    res = kb_connectors._sync_gsheets({"sheet_id": "sh1", "sheet_name": None}, None, _ctx())
    assert [d.external_id for d in res.documents] == ["2", "3"]
    assert res.documents[0].origin == "gsheet"
    assert res.documents[0].extra == {
        "gsheet_id": "sh1", "gsheet_range": "Sheet1", "gsheet_row": 2,
        "gsheet_modified": "2026-01-01T00:00:00Z",
    }
    assert res.exhaustive is True
    assert res.watermark == {"modified_time": "2026-01-01T00:00:00Z", "tab": "Sheet1"}


def test_gsheets_sync_propagates_a_fetch_error(monkeypatch):
    def boom(tid, sid, *, sheet_name, sb):
        raise RuntimeError("not connected")

    monkeypatch.setattr(gsheets, "fetch_sheet", boom)
    with pytest.raises(RuntimeError):
        kb_connectors._sync_gsheets({"sheet_id": "sh1"}, None, _ctx())


def test_gsheets_normalize_parses_the_url_and_blank_tab():
    spec = kb_connectors.get_kb_connector("gsheets")
    cfg = spec.normalize_config({
        "sheet_url": "https://docs.google.com/spreadsheets/d/ABC123def/edit#gid=0",
        "sheet_name": "  ",
    })
    assert cfg == {"sheet_id": "ABC123def", "sheet_name": None}
