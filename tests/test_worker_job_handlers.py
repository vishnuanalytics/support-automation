"""2026-09-04 -- `api/worker.py`'s `_create_github_issue` (Phase 16: a
manager approved a `task_dispatch` action in Slack -> the actual GitHub
issue gets opened here) and `_apply_kb_change` (KIL-d: a manager approved a
KB correction -> it gets written here). Both are dispatched by `HANDLERS`
(`api/worker.py`) for real jobs, but until now only the *library* functions
they call (`github.create_issue`, `kb_writeback.apply_kb_change`) had test
coverage -- the worker-level wrapper (status/idempotency checks, the Slack
card update, error handling) did not. Offline, fake `action_requests` table."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api.worker import _apply_kb_change, _create_github_issue


class _ARTable:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self._filters: dict = {}
        self._pending_update: dict | None = None

    def select(self, *_a, **_k):
        return self

    def update(self, patch: dict):
        self._pending_update = patch
        return self

    def eq(self, k, v):
        self._filters[k] = v
        return self

    def execute(self):
        matches = [r for r in self._rows if all(r.get(k) == v for k, v in self._filters.items())]
        if self._pending_update is not None:
            for r in matches:
                r.update(self._pending_update)
            self._pending_update = None
        return type("R", (), {"data": matches})()


class _SB:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def table(self, name):
        assert name == "action_requests"
        return _ARTable(self.rows)


def _ar(**over) -> dict:
    row = {"id": "ar1", "tenant_id": "t1", "kind": "github_issue", "status": "approved",
          "payload": {"repo": "acme/ops", "title": "Do the thing", "body": "b",
                      "labels": ["bot"], "assignees": []},
          "slack_channel": "#ops", "slack_ts": "111.1", "result": None}
    row.update(over)
    return row


# ── _create_github_issue ───────────────────────────────────────────────
def test_github_issue_created_and_ar_marked_done(monkeypatch):
    from api import worker

    monkeypatch.setattr(worker.githubmod, "token_for", lambda tid, sb: "gh-token")
    captured = {}

    def fake_create(token, repo, *, title, body, labels, assignees):
        captured.update(token=token, repo=repo, title=title)
        return {"html_url": "https://github.com/acme/ops/issues/9", "number": 9}
    monkeypatch.setattr(worker.githubmod, "create_issue", fake_create)
    monkeypatch.setattr(worker.slackmod, "available", lambda: True)
    updates = []
    monkeypatch.setattr(worker.slackmod, "update_message",
                        lambda tid, ch, ts, text, sb: updates.append(text))

    sb = _SB([_ar()])
    out = _create_github_issue({"action_request_id": "ar1"}, sb)

    assert captured == {"token": "gh-token", "repo": "acme/ops", "title": "Do the thing"}
    assert out == {"action_request_id": "ar1",
                   "html_url": "https://github.com/acme/ops/issues/9", "number": 9}
    assert sb.rows[0]["status"] == "done"
    assert sb.rows[0]["result"] == {"html_url": "https://github.com/acme/ops/issues/9", "number": 9}
    assert updates and "acme/ops#9" in updates[0]


def test_github_issue_missing_action_request_is_a_skip():
    out = _create_github_issue({"action_request_id": "gone"}, _SB([]))
    assert out == {"action_request_id": "gone", "skipped": "gone"}


def test_github_issue_not_approved_is_a_skip():
    sb = _SB([_ar(status="pending")])
    out = _create_github_issue({"action_request_id": "ar1"}, sb)
    assert out == {"action_request_id": "ar1", "skipped": "status=pending"}


def test_github_issue_already_done_is_idempotent():
    sb = _SB([_ar(result={"html_url": "u", "number": 1})])
    out = _create_github_issue({"action_request_id": "ar1"}, sb)
    assert out == {"action_request_id": "ar1", "idempotent_skip": True, "html_url": "u", "number": 1}


def test_github_issue_failure_marks_ar_errored_and_reraises(monkeypatch):
    from api import worker

    monkeypatch.setattr(worker.githubmod, "token_for", lambda tid, sb: "gh-token")
    monkeypatch.setattr(worker.githubmod, "create_issue",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("GitHub 403")))
    sb = _SB([_ar()])

    with pytest.raises(RuntimeError, match="GitHub 403"):
        _create_github_issue({"action_request_id": "ar1"}, sb)

    assert sb.rows[0]["status"] == "error"
    assert "GitHub 403" in sb.rows[0]["error"]


def test_github_issue_slack_update_failure_is_swallowed(monkeypatch):
    from api import worker

    monkeypatch.setattr(worker.githubmod, "token_for", lambda tid, sb: "gh-token")
    monkeypatch.setattr(worker.githubmod, "create_issue",
                        lambda *a, **k: {"html_url": "u", "number": 1})
    monkeypatch.setattr(worker.slackmod, "available", lambda: True)
    monkeypatch.setattr(worker.slackmod, "update_message",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("slack down")))

    sb = _SB([_ar()])
    out = _create_github_issue({"action_request_id": "ar1"}, sb)  # must not raise
    assert out["number"] == 1
    assert sb.rows[0]["status"] == "done"


# ── _apply_kb_change ─────────────────────────────────────────────────────
def _kb_ar(**over) -> dict:
    row = {"id": "ar2", "tenant_id": "t1", "kind": "kb_change", "status": "approved",
          "payload": {"title": "Refund window is 45 days"},
          "slack_channel": "#kb", "slack_ts": "222.2"}
    row.update(over)
    return row


def test_apply_kb_change_calls_the_library_function_and_posts(monkeypatch):
    from api import worker
    from interpreter import kb_writeback

    captured = {}

    def fake_apply(sb, ar):
        captured["ar_id"] = ar["id"]
        return {"applied": True, "entry_id": "e1"}
    monkeypatch.setattr(kb_writeback, "apply_kb_change", fake_apply)
    monkeypatch.setattr(worker.slackmod, "available", lambda: True)
    updates = []
    monkeypatch.setattr(worker.slackmod, "update_message",
                        lambda tid, ch, ts, text, sb: updates.append(text))

    sb = _SB([_kb_ar()])
    out = _apply_kb_change({"action_request_id": "ar2"}, sb)

    assert captured == {"ar_id": "ar2"}
    assert out == {"action_request_id": "ar2", "applied": True, "entry_id": "e1"}
    assert updates and "Refund window is 45 days" in updates[0]


def test_apply_kb_change_missing_action_request_is_a_skip():
    out = _apply_kb_change({"action_request_id": "gone"}, _SB([]))
    assert out == {"action_request_id": "gone", "skipped": "gone"}


def test_apply_kb_change_wrong_kind_is_a_skip():
    sb = _SB([_kb_ar(kind="github_issue")])
    out = _apply_kb_change({"action_request_id": "ar2"}, sb)
    assert out == {"action_request_id": "ar2", "skipped": "kind=github_issue"}


def test_apply_kb_change_slack_update_failure_is_swallowed(monkeypatch):
    from api import worker
    from interpreter import kb_writeback

    monkeypatch.setattr(kb_writeback, "apply_kb_change", lambda sb, ar: {"applied": True})
    monkeypatch.setattr(worker.slackmod, "available", lambda: True)
    monkeypatch.setattr(worker.slackmod, "update_message",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("slack down")))

    sb = _SB([_kb_ar()])
    out = _apply_kb_change({"action_request_id": "ar2"}, sb)  # must not raise
    assert out["applied"] is True
