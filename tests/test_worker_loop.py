"""The worker's poll loop must survive a failed iteration (offline)."""
from __future__ import annotations

import httpx
import pytest

from api import worker


class _Recorder:
    def __init__(self):
        self.sleeps: list[float] = []
        self.beats: list[dict] = []

    def sleep(self, s):
        self.sleeps.append(s)

    def beat(self, component, detail=None, *, sb=None, force=False):
        self.beats.append(detail or {})


def _script(monkeypatch, outcomes):
    """process_one returns/raises each outcome in turn."""
    it = iter(outcomes)

    def fake(_sb, **_kw):
        o = next(it)
        if isinstance(o, BaseException):
            raise o
        return o

    monkeypatch.setattr(worker, "process_one", fake)


def test_transport_error_on_claim_does_not_end_the_loop(monkeypatch):
    rec = _Recorder()
    _script(monkeypatch, [httpx.ReadTimeout("The read operation timed out"), True, False])
    worker.run_loop(object(), 2.0, beat=rec.beat, _sleep=rec.sleep, _max_iters=3)
    # error -> back off 2s; job done -> beat, no sleep; idle -> beat + idle sleep
    assert rec.sleeps == [2.0, 2.0]
    assert rec.beats == [{"idle": False}, {"idle": True}]


def test_unexpected_error_is_survived_and_logged_with_traceback(monkeypatch, caplog):
    rec = _Recorder()
    _script(monkeypatch, [RuntimeError("boom"), False])
    with caplog.at_level("ERROR", logger=worker.log.name):
        worker.run_loop(object(), 2.0, beat=rec.beat, _sleep=rec.sleep, _max_iters=2)
    assert any(r.exc_info and "RuntimeError" in r.getMessage() for r in caplog.records)
    assert rec.beats == [{"idle": True}]


def test_backoff_doubles_caps_and_resets_after_success(monkeypatch):
    rec = _Recorder()
    err = httpx.ConnectError("down")
    _script(monkeypatch, [err] * 7 + [True, err])
    worker.run_loop(object(), 2.0, beat=rec.beat, _sleep=rec.sleep, _max_iters=9)
    assert rec.sleeps == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 2.0]


def test_keyboard_interrupt_still_stops_the_worker(monkeypatch):
    rec = _Recorder()
    _script(monkeypatch, [KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        worker.run_loop(object(), 2.0, beat=rec.beat, _sleep=rec.sleep, _max_iters=1)
