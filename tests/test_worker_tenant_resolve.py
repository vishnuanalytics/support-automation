"""api/worker.py::_resolve_job_tenant — best-effort tenant attribution for
a `jobs` row that has no tenant_id (migration 099). One cheap lookup per
job kind; cross-tenant infra sweeps resolve to None."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api.worker import _resolve_job_tenant


class _Table:
    def __init__(self, rows):
        self._rows = rows
        self._f = {}

    def select(self, *a, **k):
        return self

    def eq(self, k, v):
        self._f[k] = v
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        out = [r for r in self._rows if all(r.get(k) == v for k, v in self._f.items())]
        return type("R", (), {"data": out})()


class _SB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Table(self.tables.get(name, []))


def test_direct_payload_tenant_wins_without_a_lookup():
    assert _resolve_job_tenant("run_flow", {"tenant_id": "T1"}, _SB({})) == "T1"


def test_run_flow_resolves_via_flow():
    sb = _SB({"flows": [{"flow_id": "f1", "tenant_id": "T-FLOW"}]})
    assert _resolve_job_tenant("run_flow", {"flow_id": "f1"}, sb) == "T-FLOW"


def test_check_resolution_resolves_via_run():
    sb = _SB({"runs": [{"run_id": "r1", "tenant_id": "T-RUN"}]})
    assert _resolve_job_tenant("check_resolution", {"run_id": "r1"}, sb) == "T-RUN"


def test_kb_sync_resolves_via_connection():
    sb = _SB({"kb_source_connections": [{"connection_id": "c1", "tenant_id": "T-CONN"}]})
    assert _resolve_job_tenant("kb_sync", {"connection_id": "c1"}, sb) == "T-CONN"
    assert _resolve_job_tenant("gdoc_writeback", {"connection_id": "c1"}, sb) == "T-CONN"


def test_embed_kb_entry_resolves_via_entry():
    sb = _SB({"kb_entries": [{"entry_id": "e1", "tenant_id": "T-ENTRY"}]})
    assert _resolve_job_tenant("embed_kb_entry", {"entry_id": "e1"}, sb) == "T-ENTRY"


def test_infra_sweep_has_no_tenant():
    assert _resolve_job_tenant("queue_sweep", {}, _SB({})) is None


def test_unresolvable_returns_none_not_a_crash():
    sb = _SB({"flows": []})
    assert _resolve_job_tenant("run_flow", {"flow_id": "missing"}, sb) is None
