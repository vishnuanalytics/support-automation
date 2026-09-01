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


def test_leading_bot_mention_is_stripped(monkeypatch):
    got = {}
    monkeypatch.setattr(slack_socket.reasoning, "handle_agent_message",
                        lambda _sb, _s, text, **_k: got.update(text=text) or
                        {"reply": "ok", "session": SESSION, "action": None})
    slack_socket.dispatch(_SB([dict(SESSION)]), _event(text="<@U0BT4RG2UP9> take it"),
                          post=lambda *a: None)
    assert got["text"] == "take it"


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
