"""P8c — the KIL checks added to scripts/health_check.py."""

from __future__ import annotations

import importlib
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

hc = importlib.import_module("scripts.health_check")


class _Q:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def gte(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": list(self._rows)})


class _SB:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        return _Q(self.rows if name == "review_tasks" else [])


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def test_low_flag_precision_is_flagged():
    rows = ([{"status": "correct", "created_at": _iso(20)} for _ in range(3)]
            + [{"status": "wrong", "created_at": _iso(20)} for _ in range(7)])
    problems = hc._kil_problems(_SB(rows), NOW)
    assert any("flag precision" in p for p in problems)


def test_healthy_precision_is_quiet():
    rows = ([{"status": "correct", "created_at": _iso(20)} for _ in range(9)]
            + [{"status": "wrong", "created_at": _iso(20)} for _ in range(1)])
    assert hc._kil_problems(_SB(rows), NOW) == []


def test_precision_needs_a_minimum_sample():
    # 1 correct / 2 total = 50% but only 3 resolved — below KIL_MIN_SAMPLE, stay quiet
    rows = [{"status": "correct", "created_at": _iso(20)},
            {"status": "wrong", "created_at": _iso(20)},
            {"status": "wrong", "created_at": _iso(20)}]
    assert all("flag precision" not in p for p in hc._kil_problems(_SB(rows), NOW))


def test_stale_open_queue_is_flagged():
    rows = [{"status": "open", "created_at": _iso(72)} for _ in range(hc.KIL_OPEN_MAX + 1)]
    problems = hc._kil_problems(_SB(rows), NOW)
    assert any("not being worked" in p for p in problems)


def test_recent_open_tasks_are_fine():
    rows = [{"status": "open", "created_at": _iso(2)} for _ in range(hc.KIL_OPEN_MAX + 5)]
    assert hc._kil_problems(_SB(rows), NOW) == []


def test_missing_table_is_not_a_problem():
    class _Boom:
        def table(self, *_):
            raise RuntimeError("relation \"review_tasks\" does not exist")

    assert hc._kil_problems(_Boom(), NOW) == []


def test_age_parses_z_suffix_and_bad_input():
    assert hc._age("2026-09-03T10:00:00Z", NOW) == timedelta(hours=2)
    assert hc._age("not-a-date", NOW) is None
