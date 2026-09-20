"""interpreter/observability.py -- Sentry init, off unless SENTRY_DSN is set."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import observability


def _reset(monkeypatch):
    monkeypatch.setattr(observability, "_initialized", False)


def test_init_sentry_is_a_noop_without_a_dsn(monkeypatch):
    """Must return before ever attempting `import sentry_sdk` -- doesn't
    matter whether the package is even installed if SENTRY_DSN is unset."""
    _reset(monkeypatch)
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    observability.init_sentry("api")
    assert observability._initialized is False


def test_init_sentry_initializes_and_tags_the_component(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setenv("SENTRY_DSN", "https://fake@o0.ingest.sentry.io/0")

    init_calls = []
    tag_calls = []

    class _FakeSentrySdk:
        @staticmethod
        def init(**kwargs):
            init_calls.append(kwargs)

        @staticmethod
        def set_tag(k, v):
            tag_calls.append((k, v))

    import sys as _sys
    monkeypatch.setitem(_sys.modules, "sentry_sdk", _FakeSentrySdk)

    observability.init_sentry("worker")

    assert len(init_calls) == 1
    assert init_calls[0]["dsn"] == "https://fake@o0.ingest.sentry.io/0"
    assert init_calls[0]["send_default_pii"] is False
    assert tag_calls == [("component", "worker")]
    assert observability._initialized is True


def test_init_sentry_only_initializes_once_per_process(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setenv("SENTRY_DSN", "https://fake@o0.ingest.sentry.io/0")

    init_calls = []

    class _FakeSentrySdk:
        @staticmethod
        def init(**kwargs):
            init_calls.append(kwargs)

        @staticmethod
        def set_tag(k, v):
            pass

    import sys as _sys
    monkeypatch.setitem(_sys.modules, "sentry_sdk", _FakeSentrySdk)

    observability.init_sentry("api")
    observability.init_sentry("api")  # a second call (e.g. a re-import) must not double-init
    assert len(init_calls) == 1


def test_default_traces_sample_rate_is_zero(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setenv("SENTRY_DSN", "https://fake@o0.ingest.sentry.io/0")
    monkeypatch.delenv("SENTRY_TRACES_SAMPLE_RATE", raising=False)

    init_calls = []

    class _FakeSentrySdk:
        @staticmethod
        def init(**kwargs):
            init_calls.append(kwargs)

        @staticmethod
        def set_tag(k, v):
            pass

    import sys as _sys
    monkeypatch.setitem(_sys.modules, "sentry_sdk", _FakeSentrySdk)

    observability.init_sentry("api")
    assert init_calls[0]["traces_sample_rate"] == 0.0
