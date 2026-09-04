"""2026-09-04 -- `interpreter/approvals.py` (P3/FR-44) had zero dedicated
test coverage even though it's the single place `slack_socket.dispatch_action`,
the signed Slack HTTP callback, and `/api/review-tasks/{id}/resolve` all
funnel into for deciding an approval. Offline; fake `action_requests` table,
`interpreter.jobs`/`interpreter.review`/`interpreter.kb_writeback` mocked."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import approvals


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
    row = {"id": "ar1", "kind": "github_issue", "status": "pending",
          "payload": {"title": "Open a tracking issue"},
          "slack_channel": "#ops", "slack_ts": "1.1"}
    row.update(over)
    return row


# ── decide_action_request ────────────────────────────────────────────────
def test_approve_enqueues_the_fulfilment_job(monkeypatch):
    from interpreter import jobs
    enqueued = []
    monkeypatch.setattr(jobs, "enqueue", lambda kind, payload, *, dedupe_key, sb: enqueued.append(
        (kind, payload, dedupe_key)))

    sb = _SB([_ar()])
    out = approvals.decide_action_request(sb, "ar1", approve=True, decided_by="mgr@x.com")

    assert out["status"] == "approved" and out["job_kind"] == "create_github_issue"
    assert enqueued == [("create_github_issue", {"action_request_id": "ar1"}, "github_issue:ar1")]
    assert sb.rows[0]["status"] == "approved" and sb.rows[0]["decided_by"] == "mgr@x.com"
    assert "approved by mgr@x.com" in out["slack"]["text"]
    assert out["slack"]["channel"] == "#ops" and out["slack"]["ts"] == "1.1"


def test_reject_does_not_enqueue_anything(monkeypatch):
    from interpreter import jobs
    monkeypatch.setattr(jobs, "enqueue", lambda *a, **k: pytest.fail("must not enqueue on reject"))

    sb = _SB([_ar()])
    out = approvals.decide_action_request(sb, "ar1", approve=False, decided_by="mgr@x.com")

    assert out["status"] == "rejected" and out["job_kind"] is None
    assert sb.rows[0]["status"] == "rejected"
    assert "rejected by mgr@x.com" in out["slack"]["text"]


def test_kb_change_kind_maps_to_apply_kb_change_job(monkeypatch):
    from interpreter import jobs
    enqueued = []
    monkeypatch.setattr(jobs, "enqueue", lambda kind, payload, *, dedupe_key, sb: enqueued.append(kind))

    sb = _SB([_ar(kind="kb_change")])
    out = approvals.decide_action_request(sb, "ar1", approve=True, decided_by="mgr")
    assert out["job_kind"] == "apply_kb_change" and enqueued == ["apply_kb_change"]


def test_unknown_kind_approves_without_a_job(monkeypatch):
    from interpreter import jobs
    monkeypatch.setattr(jobs, "enqueue", lambda *a, **k: pytest.fail("no job for an unknown kind"))

    sb = _SB([_ar(kind="something_new")])
    out = approvals.decide_action_request(sb, "ar1", approve=True, decided_by="mgr")
    assert out["status"] == "approved" and out["job_kind"] is None


def test_already_decided_is_a_skip_and_touches_nothing(monkeypatch):
    from interpreter import jobs
    monkeypatch.setattr(jobs, "enqueue", lambda *a, **k: pytest.fail("must not enqueue a re-decision"))

    sb = _SB([_ar(status="approved")])
    out = approvals.decide_action_request(sb, "ar1", approve=True, decided_by="mgr")
    assert out == {"skipped": "approved", "ar": sb.rows[0]}
    assert sb.rows[0]["status"] == "approved"  # untouched, not re-stamped


def test_unknown_action_request_is_a_skip():
    out = approvals.decide_action_request(_SB([]), "gone", approve=True, decided_by="mgr")
    assert out == {"skipped": "unknown", "ar_id": "gone"}


def test_enqueue_failure_does_not_break_the_decision(monkeypatch):
    from interpreter import jobs
    monkeypatch.setattr(jobs, "enqueue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("queue down")))

    sb = _SB([_ar()])
    out = approvals.decide_action_request(sb, "ar1", approve=True, decided_by="mgr")
    assert out["status"] == "approved"  # the decision still landed
    assert sb.rows[0]["status"] == "approved"


# ── resolve_review_task ──────────────────────────────────────────────────
def test_correct_drafts_and_raises_a_kb_change(monkeypatch):
    from interpreter import kb_writeback, review

    task_row = {"id": "task1", "tenant_id": "t1", "statement": "refunds take 30 days"}
    monkeypatch.setattr(review, "resolve", lambda sb, tid, *, status, reviewer_id: dict(task_row))
    monkeypatch.setattr(kb_writeback, "draft_change", lambda row: {"op": "supersede", "title": "x"})
    monkeypatch.setattr(kb_writeback, "raise_kb_change",
                        lambda sb, *, tenant_id, task_row, change: {"id": "ar-kb-1"})

    out = approvals.resolve_review_task(None, "task1", status="correct", reviewed_by="mgr")
    assert out["task"] == task_row
    assert out["kb_change"] == {"action_request_id": "ar-kb-1", "change": {"op": "supersede", "title": "x"}}


def test_wrong_and_dismissed_never_draft_a_kb_change(monkeypatch):
    from interpreter import kb_writeback, review

    monkeypatch.setattr(review, "resolve", lambda sb, tid, *, status, reviewer_id: {"id": tid})
    monkeypatch.setattr(kb_writeback, "draft_change",
                        lambda row: pytest.fail("must not draft a change for a non-correct verdict"))

    for status in ("wrong", "dismissed"):
        out = approvals.resolve_review_task(None, "task1", status=status, reviewed_by="mgr")
        assert out == {"task": {"id": "task1"}, "kb_change": None}


def test_resolve_review_task_not_open_is_a_skip(monkeypatch):
    from interpreter import review
    monkeypatch.setattr(review, "resolve", lambda sb, tid, *, status, reviewer_id: None)

    out = approvals.resolve_review_task(None, "task1", status="correct", reviewed_by="mgr")
    assert out == {"skipped": "not open", "task_id": "task1"}


def test_kb_writeback_failure_on_correct_is_swallowed(monkeypatch):
    from interpreter import kb_writeback, review

    monkeypatch.setattr(review, "resolve", lambda sb, tid, *, status, reviewer_id: {"id": tid})
    monkeypatch.setattr(kb_writeback, "draft_change",
                        lambda row: (_ for _ in ()).throw(RuntimeError("llm down")))

    out = approvals.resolve_review_task(None, "task1", status="correct", reviewed_by="mgr")  # must not raise
    assert out == {"task": {"id": "task1"}, "kb_change": None}
