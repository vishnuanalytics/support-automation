"""2026-09-04 — the 7 originally SF-hardwired node handlers (sf_writeback,
sf_case, notify, ask_human, handover, identify, clarify) plus
`alert.alert_human` (behind `notify_human`) now route their Salesforce/Slack
side effects through `interpreter.connectors.invoke()` instead of importing
`salesforce`/`slack` and calling a verb directly.

The rest of the suite (test_sf_case.py, test_notify_and_type.py,
test_case_control_plane.py, test_resilience.py, ...) already proves
*behavior* is unchanged by monkeypatching `salesforce.<verb>`/`slack.<verb>`
directly and still passing -- since connectors.py's action impls are thin
wrappers over those same functions, a patch on the underlying function still
lands. This file additionally proves *how* the call gets there: every one of
these handlers now goes through `connectors.invoke(tenant_id, "salesforce"|
"slack", "<action>", ...)`, not a direct `salesforce.<verb>`/`slack.<verb>`
call -- so a future accidental revert to a direct call is caught here even
if the underlying behavior happens to still look right."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import alert, connectors
from interpreter.registry import (
    _cp_write, h_ask_human, h_clarify, h_handover, h_identify, h_notify, h_sf_case, h_sf_writeback,
)


@pytest.fixture
def calls(monkeypatch):
    """Record every connectors.invoke(...) call registry.py/alert.py make;
    return canned, shape-correct results so each handler's own downstream
    logic (which reads the result dict) doesn't blow up."""
    seen: list[tuple[str, str, dict]] = []

    _RESULTS = {
        "update_fields": {"written": {}, "skipped": {}, "planned": {}, "dry_run": True},
        "post_note": {"posted": True, "dry_run": True, "mention_id": None},
        "add_comment": {"created": True},
        "assign_owner": {"assigned": True, "dry_run": False, "owner_id": "x", "owner_type": "queue"},
        "ensure_case": {"sf_id": "500Z", "dry_run": True},
        "log_email_message": {"created": True, "id": "em1"},
        "identify_sender": {"match": "none", "account_matched": False},
        "send_case_reply": {"sent": False, "dry_run": True, "via": "dry_run"},
        "post_message": {"sent": True, "via": "bot", "channel": "C1", "ts": "1.1"},
    }

    def fake(tenant_id, connector_slug, action_name, params, *, org_label=None, sb=None):
        seen.append((connector_slug, action_name, dict(params)))
        return _RESULTS.get(action_name, {})

    monkeypatch.setattr(connectors, "invoke", fake)
    return seen


def _names(seen):
    return {(c, a) for c, a, _ in seen}


def test_cp_write_uses_update_fields(calls):
    _cp_write({"case": {"sf_id": "500X"}, "tenant_id": "t"}, {"_node_id": "n"},
              action="test", fields={"Status": "Triaged"})
    assert ("salesforce", "update_fields") in _names(calls)


def test_sf_writeback_uses_update_fields(calls):
    h_sf_writeback({"case": {"sf_id": "500X"}, "tenant_id": "t",
                    "classification": {"topic": "billing", "urgency": "high"}},
                   {"_node_id": "n"})
    assert ("salesforce", "update_fields") in _names(calls)


def test_sf_case_uses_ensure_case_and_log_email_message(calls):
    h_sf_case({"case": {"channel": "email", "from": "a@b.com"}, "sender": {}, "tenant_id": "t"},
             {"_node_id": "n"})
    names = _names(calls)
    assert ("salesforce", "ensure_case") in names
    # dry_run ensure_case (canned result) -> log_email_message is skipped by
    # h_sf_case's own "not info.get('dry_run')" guard; assert that guard held.
    assert ("salesforce", "log_email_message") not in names


def test_identify_uses_identify_sender(calls):
    h_identify({"case": {"from": "a@b.com"}, "tenant_id": "t"}, {"_node_id": "n"})
    assert ("salesforce", "identify_sender") in _names(calls)


def test_notify_uses_post_note_and_add_comment(calls):
    h_notify({"case": {"sf_id": "500X"}, "draft": "hi",
             "classification": {"topic": "billing", "case_type": "Billing"}},
            {"_node_id": "n", "target_by_type": {"Billing": "005x"}, "use_table": False})
    names = _names(calls)
    assert ("salesforce", "post_note") in names
    assert ("salesforce", "add_comment") in names
    assert ("salesforce", "update_fields") in names  # the notify->In Progress cp_write


def test_notify_uses_update_fields_for_attention_fields(calls):
    h_notify({"case": {"sf_id": "500X"}, "draft": "",
             "classification": {"topic": "billing", "case_type": "Billing"}},
            {"_node_id": "n", "target_by_type": {"Billing": "005x"}, "use_table": False,
             "attention_fields": {"Bot_Attention__c": True}})
    calls_for = [c for c in calls if c[1] == "update_fields" and "Bot_Attention__c" in c[2].get("fields", {})]
    assert calls_for, "attention_fields must reach update_fields"


def test_ask_human_uses_post_note_add_comment_and_assign_owner(calls):
    h_ask_human({"case": {"sf_id": "500X"}, "draft": "hi", "confidence": 0.2},
               {"_node_id": "n", "queue": "Team_Support"})
    names = _names(calls)
    assert {("salesforce", "post_note"), ("salesforce", "add_comment"),
            ("salesforce", "assign_owner")} <= names


def test_handover_uses_assign_owner(calls):
    h_handover({"case": {"sf_id": "500X"}, "tier": "enterprise"},
              {"_node_id": "n", "queue": "Enterprise_Support"})
    assert ("salesforce", "assign_owner") in _names(calls)


def test_clarify_uses_send_case_reply_when_auto_send(calls):
    class _FakeSB:
        def table(self, *_): return self
        def select(self, *_): return self
        def eq(self, *_): return self
        def execute(self): return type("R", (), {"data": []})()

    h_clarify({"case": {"sf_id": "500Y", "case_id": "cid-1"}, "tenant_id": None, "draft": ""},
             {"_node_id": "cl", "auto_send": True, "_sb": _FakeSB()})
    assert ("salesforce", "send_case_reply") in _names(calls)


def test_clarify_uses_post_note_when_not_auto_send(calls):
    class _FakeSB:
        def table(self, *_): return self
        def select(self, *_): return self
        def eq(self, *_): return self
        def execute(self): return type("R", (), {"data": []})()

    h_clarify({"case": {"sf_id": "500Y", "case_id": "cid-1"}, "tenant_id": None, "draft": ""},
             {"_node_id": "cl", "auto_send": False, "_sb": _FakeSB()})
    assert ("salesforce", "post_note") in _names(calls)


def test_alert_human_uses_slack_post_message_and_sf_post_note(calls):
    out = alert.alert_human(
        {"case": {"sf_id": "500DC"}, "routed_team": "csm", "outcome": {"action": "ask_human"},
         "draft": "here is the suggested reply", "tenant_id": "t"},
        {"channel": "both", "slack_channel": "#s", "mention": {"slack_user_id": "U1"}},
    )
    names = _names(calls)
    assert ("slack", "post_message") in names
    assert ("salesforce", "post_note") in names
    del out
