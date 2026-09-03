"""
2026-09-03 -- Slack workspace introspection for the flow editor's pickers
(`notify_human.slack_channel` / `mention.slack_user_id`), mirroring
`salesforce.introspect_org`'s degrade-gracefully shape. Live-verified
against the real Globex Slack workspace before this chunk shipped (12
channels, 1 human user, 7 usergroups) -- these tests are the offline,
hermetic counterpart.

Run:  pytest tests/test_slack_introspection.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import slack


class _FakeSb:
    """Just enough of the supabase client for `_bot_token`/`connected`."""

    def __init__(self, token: str | None = "xoxb-fake"):
        self._token = token

    def table(self, name):
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def execute(self):
        data = [{"secret": {"bot_token": self._token}}] if self._token else []
        return type("R", (), {"data": data})()


def test_list_channels_filters_to_id_name_membership(monkeypatch):
    monkeypatch.setattr(slack, "_call", lambda method, token, payload: {
        "ok": True,
        "channels": [
            {"id": "C1", "name": "cx-l1", "is_member": True, "purpose": {"value": "noise"}},
            {"id": "C2", "name": "social", "is_member": False},
        ],
    })
    rows = slack.list_channels("t1", sb=_FakeSb())
    assert rows == [
        {"id": "C1", "name": "cx-l1", "is_member": True},
        {"id": "C2", "name": "social", "is_member": False},
    ]


def test_list_users_drops_bots_deleted_and_slackbot(monkeypatch):
    monkeypatch.setattr(slack, "_call", lambda method, token, payload: {
        "ok": True,
        "members": [
            {"id": "USLACKBOT", "name": "slackbot", "is_bot": False, "deleted": False},
            {"id": "U1", "name": "bot", "is_bot": True, "deleted": False},
            {"id": "U2", "name": "gone", "is_bot": False, "deleted": True},
            {"id": "U3", "name": "vish", "real_name": "Vishnu G",
             "is_bot": False, "deleted": False, "profile": {"email": "v@x.com"}},
        ],
    })
    rows = slack.list_users("t1", sb=_FakeSb())
    assert rows == [{"id": "U3", "name": "Vishnu G", "email": "v@x.com"}]


def test_list_usergroups_needs_a_handle(monkeypatch):
    monkeypatch.setattr(slack, "_call", lambda method, token, payload: {
        "ok": True,
        "usergroups": [
            {"id": "S1", "handle": "cx-l1-oncall", "name": "L1 on-call"},
            {"id": "S2", "handle": "", "name": "no handle"},
        ],
    })
    rows = slack.list_usergroups("t1", sb=_FakeSb())
    assert rows == [{"id": "S1", "handle": "cx-l1-oncall", "name": "L1 on-call"}]


def test_every_lister_degrades_to_empty_on_error(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("slack users.list: missing_scope")

    monkeypatch.setattr(slack, "_call", boom)
    assert slack.list_channels("t1", sb=_FakeSb()) == []
    assert slack.list_users("t1", sb=_FakeSb()) == []
    assert slack.list_usergroups("t1", sb=_FakeSb()) == []


def test_workspace_meta_unavailable_when_not_connected():
    m = slack.workspace_meta("t1", sb=_FakeSb(token=None))
    assert m == {"available": False, "channels": [], "users": [], "usergroups": [],
                 "errors": ["Slack not connected for this tenant"]}


def test_workspace_meta_combines_all_three_sections(monkeypatch):
    def fake_call(method, token, payload):
        if method == "conversations.list":
            return {"ok": True, "channels": [{"id": "C1", "name": "cx-l1", "is_member": True}]}
        if method == "users.list":
            return {"ok": True, "members": [
                {"id": "U3", "name": "vish", "real_name": "Vishnu G",
                 "is_bot": False, "deleted": False, "profile": {}}]}
        if method == "usergroups.list":
            return {"ok": True, "usergroups": [{"id": "S1", "handle": "oncall", "name": "On-call"}]}
        raise AssertionError(method)

    monkeypatch.setattr(slack, "_call", fake_call)
    m = slack.workspace_meta("t1", sb=_FakeSb())
    assert m["available"] is True
    assert len(m["channels"]) == 1 and len(m["users"]) == 1 and len(m["usergroups"]) == 1
    assert m["errors"] == []
