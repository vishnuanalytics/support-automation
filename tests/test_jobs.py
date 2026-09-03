"""
interpreter/jobs.py -- offline unit tests for fail()'s retry backoff.
Robustness pass (2026-09-03): fail() used to leave `run_after` untouched
on a retry, so a failed job was immediately re-claimable with zero delay.

    pytest tests/test_jobs.py
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import jobs


class _FakeSb:
    """Captures the .update(...) patch dict passed by fail()."""

    def __init__(self, attempts: int, max_attempts: int = 3):
        self._row = [{"attempts": attempts, "max_attempts": max_attempts}]
        self.patch: dict | None = None

    def table(self, name):
        return self

    def select(self, *a):
        return self

    def eq(self, *a, **k):
        return self

    def update(self, patch):
        self.patch = patch
        return self

    def execute(self):
        return type("R", (), {"data": self._row})()


def test_fail_sets_a_future_run_after_on_a_retry():
    sb = _FakeSb(attempts=1, max_attempts=3)
    jobs.fail("j1", "boom", sb=sb)
    assert sb.patch["status"] == "queued"
    assert "run_after" in sb.patch
    scheduled = datetime.fromisoformat(sb.patch["run_after"])
    assert scheduled > datetime.now(timezone.utc)


def test_fail_backoff_grows_with_attempts():
    sb1 = _FakeSb(attempts=1, max_attempts=5)
    jobs.fail("j1", "boom", sb=sb1)
    delay1 = (datetime.fromisoformat(sb1.patch["run_after"])
              - datetime.now(timezone.utc)).total_seconds()

    sb2 = _FakeSb(attempts=3, max_attempts=5)
    jobs.fail("j1", "boom", sb=sb2)
    delay2 = (datetime.fromisoformat(sb2.patch["run_after"])
              - datetime.now(timezone.utc)).total_seconds()

    assert delay2 > delay1


def test_fail_backoff_is_capped():
    sb = _FakeSb(attempts=20, max_attempts=25)
    jobs.fail("j1", "boom", sb=sb)
    delay = (datetime.fromisoformat(sb.patch["run_after"])
             - datetime.now(timezone.utc)).total_seconds()
    assert delay <= jobs._BACKOFF_CAP_SECONDS + 1  # +1s test-runtime slack


def test_fail_once_attempts_are_exhausted_sets_failed_with_no_run_after():
    sb = _FakeSb(attempts=3, max_attempts=3)
    jobs.fail("j1", "boom", sb=sb)
    assert sb.patch["status"] == "failed"
    assert "run_after" not in sb.patch


def test_fail_with_no_job_id_is_a_noop():
    sb = _FakeSb(attempts=1)
    jobs.fail("", "boom", sb=sb)
    assert sb.patch is None
