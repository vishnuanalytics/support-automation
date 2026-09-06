"""KB source connectors (docs/KB_SOURCE_CONNECTORS.md): the KBConnectorSpec
registry and the generic `api/worker.py::_sync_kb_connection` driver that
every connector shares (diff / upsert / embed / archive / watermark).

Offline: Supabase and the job queue are mocked; a throwaway connector is
registered so the driver tests don't depend on crawl / Sheets internals.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api import worker
from interpreter import kb_connectors
from interpreter.kb_connectors import KBConnectorSpec, KBDocument, KBSyncResult


# ── a fake Supabase good enough for the driver (eq filter, insert, update) ──
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

    def neq(self, k, v):
        self._filters[("neq", k)] = v
        return self

    def limit(self, _n):
        return self

    def order(self, *_a, **_k):
        return self

    def insert(self, row):
        self._pending = ("insert", dict(row))
        return self

    def update(self, patch):
        self._pending = ("update", dict(patch))
        return self

    def _match(self, r):
        for k, v in self._filters.items():
            if isinstance(k, tuple) and k[0] == "neq":
                if r.get(k[1]) == v:
                    return False
            elif r.get(k) != v:
                return False
        return True

    def execute(self):
        op, data = self._pending or (None, None)
        if op == "insert":
            new_row = {"entry_id": f"e{len(self.rows) + 1}", **data}
            self.rows.append(new_row)
            self._pending, self._filters = None, {}
            return type("R", (), {"data": [new_row]})()
        matches = [r for r in self.rows if self._match(r)]
        if op == "update":
            for r in matches:
                r.update(data)
        self._pending, self._filters = None, {}
        return type("R", (), {"data": matches})()


class _SB:
    def __init__(self, *, connections=None, kb_entries=None, sources=None):
        self._t = {
            "kb_source_connections": _Table(connections or []),
            "kb_entries": _Table(kb_entries or []),
            "sources": _Table(sources or [{"source_id": "s", "name": "col"}]),
        }

    def table(self, name):
        return self._t[name]


_HOLDER: dict = {}


def _reg_test_connector():
    kb_connectors.register(KBConnectorSpec(
        slug="_test", label="Test", auth="none",
        sync=lambda config, wm, ctx: _HOLDER["fn"](config, wm, ctx),
    ))


_reg_test_connector()


def _conn(**over):
    row = {"connection_id": "c1", "source_id": "s", "tenant_id": "t",
           "connector": "_test", "config": {}, "watermark": None, "status": "active",
           "created_by": "u1"}
    row.update(over)
    return row


def _run(sb, monkeypatch, docs=None, *, exhaustive=True, watermark=None, raises=None):
    enq = []
    monkeypatch.setattr("interpreter.jobs.enqueue",
                        lambda kind, payload, **kw: enq.append((kind, payload)) or "job1")

    def fn(config, wm, ctx):
        if raises:
            raise raises
        return KBSyncResult(documents=docs or [], exhaustive=exhaustive, watermark=watermark)

    _HOLDER["fn"] = fn
    out = worker._sync_kb_connection({"connection_id": "c1", "collection_name": "col"}, sb)
    return out, enq


# ── registry ──────────────────────────────────────────────────────────────
def test_registry_has_the_three_builtins():
    slugs = {s.slug for s in kb_connectors.list_kb_connectors()}
    assert {"public_url", "gdocs", "gsheets"} <= slugs


def test_get_unknown_connector_raises():
    with pytest.raises(KeyError):
        kb_connectors.get_kb_connector("nope")


def test_google_connectors_report_unavailable_without_a_connection(monkeypatch):
    monkeypatch.setattr("interpreter.gdrive.available", lambda: True)
    monkeypatch.setattr("interpreter.gdrive.connected", lambda tid, sb: False)
    ok, reason = kb_connectors.get_kb_connector("gsheets").is_available("t", None)
    assert ok is False and "Connect Google" in reason


# ── the generic driver ───────────────────────────────────────────────────
def test_driver_creates_one_entry_per_document(monkeypatch):
    sb = _SB(connections=[_conn()])
    docs = [KBDocument(external_id="P1", title="P1", body_md="a", origin="crawl"),
            KBDocument(external_id="P2", title="P2", body_md="b", origin="crawl")]
    out, enq = _run(sb, monkeypatch, docs)

    assert out["entries"] == 2 and out["unchanged"] == 0 and out["archived"] == 0
    rows = sb.table("kb_entries").rows
    assert {r["external_id"] for r in rows} == {"P1", "P2"}
    assert all(r["connection_id"] == "c1" and r["source_id"] == "s" for r in rows)
    assert [k for k, _ in enq] == ["embed_kb_entry", "embed_kb_entry"]
    conn = sb.table("kb_source_connections").rows[0]
    assert conn["status"] == "active"
    assert conn["last_result"] == {"documents": 2, "entries": 2, "unchanged": 0, "archived": 0}
    assert conn["last_synced_at"] == "now()"


def test_driver_skips_a_byte_identical_document(monkeypatch):
    sb = _SB(
        connections=[_conn()],
        kb_entries=[{"entry_id": "e_old", "connection_id": "c1", "external_id": "P1",
                     "body_md": "same", "status": "active"}],
    )
    docs = [KBDocument(external_id="P1", title="P1", body_md="same"),
            KBDocument(external_id="P2", title="P2", body_md="new")]
    out, enq = _run(sb, monkeypatch, docs)
    assert out["unchanged"] == 1 and out["entries"] == 1
    assert len(enq) == 1


def test_driver_merges_doc_extra_into_the_row(monkeypatch):
    sb = _SB(connections=[_conn()])
    docs = [KBDocument(external_id="7", title="Row 7", body_md="x", origin="gsheet",
                       extra={"gsheet_id": "sh", "gsheet_row": 7})]
    _run(sb, monkeypatch, docs)
    row = sb.table("kb_entries").rows[0]
    assert row["gsheet_id"] == "sh" and row["gsheet_row"] == 7 and row["origin"] == "gsheet"


def test_driver_archives_a_vanished_document_when_exhaustive(monkeypatch):
    sb = _SB(
        connections=[_conn()],
        kb_entries=[
            {"entry_id": "e_keep", "connection_id": "c1", "external_id": "P1",
             "body_md": "old", "status": "active"},
            {"entry_id": "e_gone", "connection_id": "c1", "external_id": "P_gone",
             "body_md": "z", "status": "active"},
        ],
    )
    out, _ = _run(sb, monkeypatch,
                  [KBDocument(external_id="P1", title="P1", body_md="new")], exhaustive=True)
    assert out["archived"] == 1
    by_id = {r["entry_id"]: r for r in sb.table("kb_entries").rows}
    assert by_id["e_gone"]["status"] == "archived"
    assert by_id["e_keep"]["status"] == "active"


def test_driver_does_not_archive_when_not_exhaustive(monkeypatch):
    sb = _SB(
        connections=[_conn()],
        kb_entries=[{"entry_id": "e_maybe", "connection_id": "c1", "external_id": "P_x",
                     "body_md": "z", "status": "active"}],
    )
    out, _ = _run(sb, monkeypatch,
                  [KBDocument(external_id="P1", title="P1", body_md="a")], exhaustive=False)
    assert out["archived"] == 0
    assert sb.table("kb_entries").rows[0]["status"] == "active"


def test_driver_persists_the_watermark(monkeypatch):
    sb = _SB(connections=[_conn()])
    _run(sb, monkeypatch, [], watermark={"cursor": "abc"})
    assert sb.table("kb_source_connections").rows[0]["watermark"] == {"cursor": "abc"}


def test_driver_marks_the_connection_errored_when_sync_raises(monkeypatch):
    sb = _SB(connections=[_conn()])
    out, enq = _run(sb, monkeypatch, raises=RuntimeError("boom"))
    assert "error" in out and "boom" in out["error"]
    conn = sb.table("kb_source_connections").rows[0]
    assert conn["status"] == "error" and conn["last_result"]["error"].startswith("boom")
    assert enq == []


def test_driver_skips_a_paused_connection(monkeypatch):
    sb = _SB(connections=[_conn(status="paused")])
    called = []
    _HOLDER["fn"] = lambda *a: called.append(1)
    monkeypatch.setattr("interpreter.jobs.enqueue", lambda *a, **k: "job1")
    out = worker._sync_kb_connection({"connection_id": "c1"}, sb)
    assert out["skipped"] == "status=paused"
    assert called == []


def test_driver_no_op_when_the_connection_is_gone(monkeypatch):
    out = worker._sync_kb_connection({"connection_id": "missing"}, _SB())
    assert out["skipped"] == "connection gone"
