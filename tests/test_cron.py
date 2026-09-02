"""P6b — the tiny cron matcher."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import cron

UTC = timezone.utc


def dt(y=2026, mo=9, d=2, h=12, mi=0):        # 2026-09-02 is a Wednesday
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def test_field_parsing_forms():
    assert cron._field("*/15", 0, 59) == {0, 15, 30, 45}
    assert cron._field("1-5", 0, 59) == {1, 2, 3, 4, 5}
    assert cron._field("0,30", 0, 59) == {0, 30}
    assert cron._field("10-20/5", 0, 59) == {10, 15, 20}
    assert cron._field("*", 0, 6) == {0, 1, 2, 3, 4, 5, 6}


def test_parse_rejects_wrong_arity():
    with pytest.raises(ValueError):
        cron.parse("* * * *")


def test_matches_minute_and_hour():
    assert cron.matches("*/15 * * * *", dt(mi=15))
    assert cron.matches("*/15 * * * *", dt(mi=0))
    assert not cron.matches("*/15 * * * *", dt(mi=7))
    assert cron.matches("0 9 * * *", dt(h=9, mi=0))
    assert not cron.matches("0 9 * * *", dt(h=10, mi=0))


def test_matches_weekday_range():
    # 2026-09-02 = Wed (cron dow 3); 2026-09-05 = Sat (6)
    assert cron.matches("0 9 * * 1-5", dt(d=2, h=9, mi=0))
    assert not cron.matches("0 9 * * 1-5", dt(d=5, h=9, mi=0))
    assert cron.matches("0 9 * * 6", dt(d=5, h=9, mi=0))


def test_dow_7_is_sunday():
    # 2026-09-06 = Sunday
    assert cron.matches("30 8 * * 7", dt(d=6, h=8, mi=30))
    assert cron.matches("30 8 * * 0", dt(d=6, h=8, mi=30))


def test_dom_and_dow_both_restricted_is_or():
    # "1st of the month OR any Monday"
    expr = "0 0 1 * 1"
    assert cron.matches(expr, dt(d=1, h=0, mi=0))          # the 1st (a Tuesday)
    assert cron.matches(expr, dt(d=7, h=0, mi=0))          # 2026-09-07 is a Monday
    assert not cron.matches(expr, dt(d=2, h=0, mi=0))      # Wed, not the 1st


def test_due_fires_once_within_the_lookback():
    now = dt(h=9, mi=3)
    assert cron.due("0 9 * * *", None, now)                 # 09:00 is in the last 60 min
    assert not cron.due("0 9 * * *", dt(h=9, mi=0), now)    # already fired at 09:00
    assert cron.due("0 9 * * *", dt(h=8, mi=0), now)        # last fire was before 09:00


def test_due_caps_the_lookback_after_downtime():
    now = dt(h=15, mi=0)
    # last fired days ago; an hourly cron should still only report "due" once now
    assert cron.due("0 * * * *", now - timedelta(days=2), now)
    assert not cron.due("5 * * * *", now, now)              # nothing matches :00..:00
