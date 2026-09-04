"""Phase 24c — the Socket Mode dispatcher (offline; no websocket, no Slack)."""

from __future__ import annotations

import pytest

from interpreter import slack_socket


class _Q:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    @property
    def not_(self):
        return self

    def in_(self, *_a, **_k):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})


class _SB:
    def __init__(self, session_rows):
        self._rows = session_rows

    def table(self, name):
        return _Q(self._rows if name == "reasoning_sessions" else [])


SESSION = {"session_id": "s1", "case_id": "500X", "tenant_id": "t",
           "state": "reasoning", "slack_thread_ts": "111.1"}


def _event(**over):
    e = {"type": "message", "channel": "C1", "user": "UAGENT",
         "text": "an amount", "ts": "222.2", "thread_ts": "111.1"}
    e.update(over)
    return e


def test_ignores_bot_and_non_message_events():
    sb = _SB([SESSION])
    posts = []
    p = lambda c, t, x: posts.append((c, t, x))
    assert "skip" in slack_socket.dispatch(sb, _event(bot_id="B1"), post=p)
    assert "skip" in slack_socket.dispatch(sb, {"type": "reaction_added"}, post=p)
    assert "skip" in slack_socket.dispatch(sb, _event(subtype="message_changed"), post=p)
    assert "skip" in slack_socket.dispatch(sb, _event(user="UBOT"), post=p, bot_user_id="UBOT")
    assert not posts


def test_no_session_for_thread_is_a_noop():
    sb = _SB([])
    posts = []
    out = slack_socket.dispatch(sb, _event(), post=lambda *a: posts.append(a))
    assert out["skip"] == "no open session for this thread"
    assert not posts


def test_a_turn_posts_the_engine_reply(monkeypatch):
    sb = _SB([dict(SESSION)])
    monkeypatch.setattr(slack_socket.reasoning, "handle_agent_message",
                        lambda _sb, _s, _t, **_k: {"reply": "Noted. Next: 2/4 …",
                                                   "session": {**SESSION, "state": "reasoning"},
                                                   "action": None})
    posts = []
    out = slack_socket.dispatch(sb, _event(), post=lambda c, t, x: posts.append((c, t, x)))
    assert posts == [("C1", "111.1", "Noted. Next: 2/4 …")]
    assert out["state"] == "reasoning" and out["action"] is None


def test_leading_bot_mention_is_stripped_and_flags_handoff(monkeypatch):
    got = {}
    monkeypatch.setattr(slack_socket.reasoning, "handle_agent_message",
                        lambda _sb, _s, text, **kw: got.update(text=text, kw=kw) or
                        {"reply": "ok", "session": SESSION, "action": None})
    slack_socket.dispatch(_SB([dict(SESSION)]), _event(text="<@U0BT4RG2UP9> take it"),
                          post=lambda *a: None, bot_user_id="U0BT4RG2UP9")
    assert got["text"] == "take it"
    assert got["kw"]["handoff"] is True


def test_bare_at_mention_still_dispatches(monkeypatch):
    got = {}
    monkeypatch.setattr(slack_socket.reasoning, "handle_agent_message",
                        lambda _sb, _s, text, **kw: got.update(text=text, kw=kw) or
                        {"reply": "ok", "session": SESSION, "action": None})
    out = slack_socket.dispatch(_SB([dict(SESSION)]), _event(text="<@UBOT>"),
                                post=lambda *a: None, bot_user_id="UBOT")
    assert "skip" not in out and got["text"] == "" and got["kw"]["handoff"] is True


def test_approval_triggers_delivery_and_confirms(monkeypatch):
    sb = _SB([dict(SESSION)])
    monkeypatch.setattr(slack_socket.reasoning, "handle_agent_message",
                        lambda _sb, _s, _t, **_k: {"reply": "Sending now. ✅",
                                                   "session": {**SESSION, "state": "sent",
                                                               "draft": "the reply"},
                                                   "action": "send"})
    posts = []
    delivered = {}
    out = slack_socket.dispatch(
        sb, _event(text="looks good send it"),
        post=lambda c, t, x: posts.append(x),
        deliver=lambda _sb, sess: delivered.update(sess) or {"sent": True, "via": "smtp"})
    assert delivered["draft"] == "the reply"
    assert out["action"] == "send" and out["delivery"]["sent"] is True
    assert posts[0] == "Sending now. ✅"
    assert "Sent to the customer" in posts[1]


def test_delivery_failure_is_reported_in_thread(monkeypatch):
    monkeypatch.setattr(slack_socket.reasoning, "handle_agent_message",
                        lambda *a, **k: {"reply": "Sending now. ✅",
                                         "session": {**SESSION, "state": "sent"},
                                         "action": "send"})
    posts = []
    slack_socket.dispatch(_SB([dict(SESSION)]), _event(),
                          post=lambda c, t, x: posts.append(x),
                          deliver=lambda *a: {"sent": False, "error": "smtp auth"})
    assert "couldn't send it" in posts[1] and "smtp auth" in posts[1]


# ── Phase 27h — Block Kit button clicks ───────────────────────────
def _payload(action_id, **over):
    p = {"type": "block_actions", "actions": [{"action_id": action_id}],
         "channel": {"id": "C1"}, "container": {"thread_ts": "111.1"},
         "message": {"ts": "111.1"}, "user": {"id": "UAGENT"}}
    p.update(over)
    return p


def test_action_send_delivers(monkeypatch):
    monkeypatch.setattr(slack_socket.reasoning, "handle_agent_message",
                        lambda *a, **k: {"reply": "ok", "session": dict(SESSION),
                                         "action": "send"})
    posts = []
    out = slack_socket.dispatch_action(
        _SB([dict(SESSION)]), _payload("cx_send"),
        post=lambda c, t, x: posts.append(x),
        deliver=lambda *a: {"sent": True, "via": "smtp"})
    assert out["action"] == "send" and out["delivery"]["sent"] is True
    assert "Sent to the customer" in posts[0]


def test_action_edit_and_reassign_prompt(monkeypatch):
    posts = []
    p = lambda c, t, x: posts.append(x)  # noqa: E731
    o1 = slack_socket.dispatch_action(_SB([dict(SESSION)]), _payload("cx_edit"), post=p)
    o2 = slack_socket.dispatch_action(_SB([dict(SESSION)]), _payload("cx_not_my_team"), post=p)
    assert o1["action"] == "edit" and "edited reply text" in posts[0]
    assert o2["action"] == "cx_not_my_team" and "route: <team>" in posts[1]


def test_action_no_session_is_a_skip():
    out = slack_socket.dispatch_action(_SB([]), _payload("cx_send"), post=lambda *a: None)
    assert out["skip"] == "no open session for this thread"


def test_route_command_reassigns(monkeypatch):
    called = {}
    monkeypatch.setattr(slack_socket, "_reassign",
                        lambda sb, sess, team: called.update(team=team) or {"ok": True})
    posts = []
    out = slack_socket.dispatch(_SB([dict(SESSION)]), _event(text="route: tier2"),
                                post=lambda c, t, x: posts.append(x))
    assert called["team"] == "tier2" and out["routed_team"] == "tier2"
    assert "Re-routed to *tier2*" in posts[0]


def test_redrive_nudges_open_sessions():
    posts = []
    rows = [
        {"session_id": "a", "slack_channel": "#x", "slack_thread_ts": "1.1", "state": "clarifying"},
        {"session_id": "b", "slack_channel": "#x", "slack_thread_ts": "2.2", "state": "drafting"},
        {"session_id": "c", "slack_channel": None, "slack_thread_ts": None, "state": "clarifying"},
    ]
    slack_socket._redrive_open_sessions(_SB(rows), lambda c, t, x: posts.append((c, t)))
    assert posts == [("#x", "1.1"), ("#x", "2.2")]      # the channelless one is skipped


# --------------------------------------------------------------------------
# 2026-09-04 -- the KIL-c human-reply-review card and the KIL-d/Phase-16
# action_requests approval card. Previously "live-verified once" only --
# these branches (`dispatch_action`'s review_*/kb_approve/kb_reject/approve/
# reject handling) had zero offline coverage even though the reasoning-
# session action branches above (cx_send/cx_edit/...) did.
# --------------------------------------------------------------------------
def _action_value(action_id: str, value: str):
    return _payload(action_id, actions=[{"action_id": action_id, "value": value}])


def test_review_correct_resolves_and_posts(monkeypatch):
    from interpreter import approvals

    monkeypatch.setattr(approvals, "resolve_review_task",
                        lambda sb, tid, *, status, reviewed_by: {"task": {"id": tid}, "kb_change": None})
    posts = []
    out = slack_socket.dispatch_action(None, _action_value("review_correct", "task-1"),
                                       post=lambda c, t, x: posts.append(x))
    assert out == {"action": "review_correct", "task": "task-1", "status": "correct"}
    assert "drafting a KB update" in posts[0]


def test_review_wrong_and_dismissed_post_the_right_note(monkeypatch):
    from interpreter import approvals

    monkeypatch.setattr(approvals, "resolve_review_task",
                        lambda sb, tid, *, status, reviewed_by: {"task": {"id": tid}})
    posts = []
    slack_socket.dispatch_action(None, _action_value("review_wrong", "t2"),
                                 post=lambda c, t, x: posts.append(x))
    assert "coaching" in posts[0]

    posts.clear()
    slack_socket.dispatch_action(None, _action_value("review_dismiss", "t3"),
                                 post=lambda c, t, x: posts.append(x))
    assert "Dismissed" in posts[0]


def test_review_action_already_resolved_is_a_skip(monkeypatch):
    from interpreter import approvals

    monkeypatch.setattr(approvals, "resolve_review_task",
                        lambda *a, **k: {"skipped": "not open", "task_id": "t1"})
    posts = []
    out = slack_socket.dispatch_action(None, _action_value("review_correct", "t1"),
                                       post=lambda c, t, x: posts.append(x))
    assert out == {"skip": "review task not open", "action": "review_correct"}
    assert "already resolved" in posts[0]


def test_review_missing_task_id_is_a_skip_without_calling_resolve(monkeypatch):
    from interpreter import approvals

    def _boom(*a, **k):
        raise AssertionError("resolve_review_task must not be called with no task id")
    monkeypatch.setattr(approvals, "resolve_review_task", _boom)
    out = slack_socket.dispatch_action(None, _action_value("review_correct", ""),
                                       post=lambda c, t, x: None)
    assert out["skip"] == "review task not open"


def test_kb_approve_calls_decide_action_request_and_posts(monkeypatch):
    from interpreter import approvals

    captured = {}

    def fake_decide(sb, ar_id, *, approve, decided_by):
        captured.update(ar_id=ar_id, approve=approve, decided_by=decided_by)
        return {"status": "approved", "slack": {"channel": "C1", "ts": "1", "text": "approved!"}}

    monkeypatch.setattr(approvals, "decide_action_request", fake_decide)
    posts = []
    out = slack_socket.dispatch_action(None, _action_value("kb_approve", "ar-1"),
                                       post=lambda c, t, x: posts.append(x))
    assert captured == {"ar_id": "ar-1", "approve": True, "decided_by": "UAGENT"}
    assert out == {"action": "kb_approve", "action_request": "ar-1", "status": "approved"}
    assert "approved!" in posts[0]


def test_kb_reject_passes_approve_false(monkeypatch):
    from interpreter import approvals

    captured = {}

    def fake_decide(sb, ar_id, *, approve, decided_by):
        captured["approve"] = approve
        return {"status": "rejected", "slack": {"text": "rejected."}}

    monkeypatch.setattr(approvals, "decide_action_request", fake_decide)
    slack_socket.dispatch_action(None, _action_value("kb_reject", "ar-2"), post=lambda c, t, x: None)
    assert captured["approve"] is False


def test_generic_approve_and_reject_aliases_also_dispatch(monkeypatch):
    """The unified-approvals web tab (`interpreter/approvals.py`, P4) posts
    the generic `approve`/`reject` action_ids, not just KIL-d's
    kb_approve/kb_reject -- both must resolve to the same handler."""
    from interpreter import approvals

    captured = {}
    monkeypatch.setattr(approvals, "decide_action_request",
                        lambda sb, ar_id, *, approve, decided_by:
                        captured.update(approve=approve) or {"status": "x", "slack": {"text": "ok"}})
    slack_socket.dispatch_action(None, _action_value("approve", "ar-3"), post=lambda c, t, x: None)
    assert captured["approve"] is True

    slack_socket.dispatch_action(None, _action_value("reject", "ar-3"), post=lambda c, t, x: None)
    assert captured["approve"] is False


def test_kb_action_already_decided_is_a_skip(monkeypatch):
    from interpreter import approvals

    monkeypatch.setattr(approvals, "decide_action_request", lambda *a, **k: {"skipped": "approved"})
    posts = []
    out = slack_socket.dispatch_action(None, _action_value("kb_approve", "ar-4"),
                                       post=lambda c, t, x: posts.append(x))
    assert out == {"skip": "action_request approved", "action": "kb_approve"}
    assert "Already decided" in posts[0]
