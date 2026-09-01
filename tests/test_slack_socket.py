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
