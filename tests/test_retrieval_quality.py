"""interpreter.retrieval._apply_quality_weights — the source-trust nudge
(migration 097). Offline: `sb` is a stub that returns fixed qualities."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter.retrieval import _apply_quality_weights


class _SB:
    def __init__(self, qmap):
        self._q = qmap
        self._ids: list[str] = []

    def table(self, _):
        return self

    def select(self, *_a, **_k):
        return self

    def in_(self, _k, ids):
        self._ids = list(ids)
        return self

    def execute(self):
        data = [{"entry_id": i, "quality": self._q.get(i, "unverified")} for i in self._ids]
        return type("R", (), {"data": data})()


def _row(eid, rrf, url=None):
    return {"doc_url": url or f"kb://s1/{eid}", "_rrf": rrf, "chunk_id": eid}


def test_official_is_weighted_up_and_rows_are_resorted():
    rows = [_row("A", 0.10), _row("B", 0.11)]   # B ahead before weighting
    _apply_quality_weights(_SB({"A": "official", "B": "unverified"}), rows)
    # A: 0.10*1.15 = 0.115 ; B: 0.11*0.9 = 0.099  -> A now first
    assert [r["chunk_id"] for r in rows] == ["A", "B"]
    assert abs(rows[0]["_rrf"] - 0.115) < 1e-9
    assert abs(rows[1]["_rrf"] - 0.099) < 1e-9


def test_non_kb_urls_are_left_at_weight_one():
    rows = [_row("x", 0.2, url="https://docs.zapier.com/a")]
    _apply_quality_weights(_SB({}), rows)
    assert rows[0]["_rrf"] == 0.2


def test_community_resolved_is_neutral():
    rows = [_row("C", 0.3)]
    _apply_quality_weights(_SB({"C": "community_resolved"}), rows)
    assert rows[0]["_rrf"] == 0.3


def test_a_lookup_failure_is_swallowed():
    class _Boom:
        def table(self, _):
            raise RuntimeError("db down")

    rows = [_row("A", 0.5)]
    _apply_quality_weights(_Boom(), rows)   # must not raise
    assert rows[0]["_rrf"] == 0.5
