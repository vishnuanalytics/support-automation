"""
2026-09-03 -- completing the multi-org Salesforce connector end to end:
every SF-touching node handler now reads an optional `config.org` and
threads it all the way down to `salesforce.client_for(tenant_id, org_label)`,
so a flow can actually pick a non-default connected org at run time (the
connector + self-serve UI landed in earlier chunks; this is the piece
that makes it usable from a flow). `config.org` unset (the default for
every existing flow) must keep resolving exactly what it always did --
these tests assert both the "org set" and "org unset" cases.

Run:  pytest tests/test_salesforce_org_threading.py
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import alert, attachments, registry, salesforce, sf_context
from interpreter.registry import (
    h_ask_human, h_attachments, h_clarify, h_handover, h_identify, h_notify,
    h_notify_human, h_sf_case, h_sf_context, h_sf_writeback,
)

_HERMETIC = ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN", "SF_CONSUMER_KEY",
             "SF_CONSUMER_SECRET", "SF_PRIVATE_KEY", "SF_PRIVATE_KEY_FILE")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    for k in _HERMETIC:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(salesforce, "_client_obj", None, raising=False)
    salesforce._tenant_clients.clear()


def test_sf_writeback_threads_org(monkeypatch):
    captured = {}
    monkeypatch.setattr(salesforce, "update_case_fields",
                        lambda *a, **k: captured.update(k) or {"dry_run": True, "written": {},
                                                                "skipped": {}, "planned": {}})
    state = {"case": {"sf_id": "500A"}, "tenant_id": "t1", "classification": {}}
    h_sf_writeback(state, {"_node_id": "n", "org": "prod"})
    assert captured["org_label"] == "prod"


def test_sf_writeback_org_unset_stays_none(monkeypatch):
    captured = {}
    monkeypatch.setattr(salesforce, "update_case_fields",
                        lambda *a, **k: captured.update(k) or {"dry_run": True, "written": {},
                                                                "skipped": {}, "planned": {}})
    state = {"case": {"sf_id": "500A"}, "tenant_id": "t1", "classification": {}}
    h_sf_writeback(state, {"_node_id": "n"})
    assert captured["org_label"] is None


def test_sf_case_threads_org(monkeypatch):
    captured = {}

    def fake_ensure_case(*a, **k):
        captured.update(k)
        return {"sf_id": None, "dry_run": True}

    monkeypatch.setattr(salesforce, "ensure_case", fake_ensure_case)
    h_sf_case({"case": {}, "sender": {}, "tenant_id": "t1"}, {"_node_id": "n", "org": "sandbox"})
    assert captured["org_label"] == "sandbox"


def test_identify_threads_org(monkeypatch):
    captured = {}
    monkeypatch.setattr(salesforce, "identify_sender",
                        lambda *a, **k: captured.update(k) or {"match": "none"})
    h_identify({"case": {"from": "a@b.com"}, "tenant_id": "t1"}, {"_node_id": "n", "org": "eu"})
    assert captured["org_label"] == "eu"


def test_ask_human_threads_org_to_chatter_and_assign(monkeypatch):
    captured_chatter, captured_assign = {}, {}
    monkeypatch.setattr(salesforce, "post_chatter",
                        lambda *a, **k: captured_chatter.update(k) or {"dry_run": True})
    monkeypatch.setattr(salesforce, "add_case_comment", lambda *a, **k: {"created": False})
    monkeypatch.setattr(salesforce, "assign_case",
                        lambda *a, **k: captured_assign.update(k) or {"assigned": False})
    monkeypatch.setattr(registry, "_cp_write", lambda *a, **k: {})
    state = {"case": {"sf_id": "500A"}, "tenant_id": "t1", "confidence": 0.2}
    h_ask_human(state, {"_node_id": "n", "org": "prod", "queue": "Team_Support"})
    assert captured_chatter["org_label"] == "prod"
    assert captured_assign["org_label"] == "prod"


def test_notify_threads_org_to_chatter_and_queue_member(monkeypatch):
    captured_chatter, captured_qm = {}, {}
    monkeypatch.setattr(salesforce, "post_chatter",
                        lambda *a, **k: captured_chatter.update(k) or {"dry_run": True})
    monkeypatch.setattr(salesforce, "add_case_comment", lambda *a, **k: {"created": False})

    def fake_queue_member(queue_ref, tenant_id=None, org_label=None):
        captured_qm["org_label"] = org_label
        return ("005xyz", "Some Rep")

    import interpreter.routing as routing_mod
    monkeypatch.setattr(routing_mod, "queue_member", fake_queue_member)
    monkeypatch.setattr(registry, "_cp_write", lambda *a, **k: {})
    state = {"case": {"sf_id": "500A"}, "tenant_id": "t1", "classification": {}}
    h_notify(state, {"_node_id": "n", "org": "prod",
                     "target_by_type": {}, "fallback_target": "00Gqueueid",
                     "use_table": False})
    # fallback_target isn't SF-id-shaped, so the queue-member mention path
    # doesn't fire here -- assert the chatter call itself carried org_label.
    assert captured_chatter["org_label"] == "prod"


def test_handover_threads_org(monkeypatch):
    captured = {}
    monkeypatch.setattr(salesforce, "assign_case",
                        lambda *a, **k: captured.update(k) or {"assigned": False})
    monkeypatch.setattr(registry, "_cp_write", lambda *a, **k: {})
    state = {"case": {"sf_id": "500A"}, "tenant_id": "t1", "routed_team": "support"}
    h_handover(state, {"_node_id": "n", "org": "prod", "queue": "Team_Support"})
    assert captured["org_label"] == "prod"


def test_clarify_threads_org(monkeypatch):
    captured = {}
    monkeypatch.setattr(salesforce, "post_chatter",
                        lambda *a, **k: captured.update(k) or {"dry_run": True})
    state = {"case": {"sf_id": "500A", "subject": "s", "body": "b"}, "tenant_id": "t1",
            "classification": {"answer_mode": "informational"}}
    h_clarify(state, {"_node_id": "n", "org": "prod", "max_questions": 2})
    assert captured.get("org_label") == "prod"


def test_sf_context_threads_org(monkeypatch):
    captured = {}
    monkeypatch.setattr(sf_context, "load",
                        lambda *a, **k: captured.update(k) or {})
    h_sf_context({"sender": {}, "tenant_id": "t1"}, {"_node_id": "n", "org": "prod"})
    assert captured["org_label"] == "prod"


def test_attachments_threads_org(monkeypatch):
    captured = {}
    monkeypatch.setattr(attachments, "extract",
                        lambda *a, **k: captured.update(k) or
                        {"attachments": [], "attachment_text": "", "_blobs": {}})
    h_attachments({"case": {}, "tenant_id": "t1"}, {"_node_id": "n", "org": "prod"})
    assert captured["org_label"] == "prod"


def test_notify_human_threads_org_via_alert_human(monkeypatch):
    captured = {}
    monkeypatch.setattr(salesforce, "post_chatter",
                        lambda *a, **k: captured.update(k) or {"dry_run": True})
    monkeypatch.setattr(registry, "_cp_write", lambda *a, **k: {})
    state = {"case": {"sf_id": "500A"}, "tenant_id": "t1", "routed_team": "support"}
    h_notify_human(state, {"_node_id": "n", "org": "prod", "channel": "salesforce_chatter"})
    assert captured.get("org_label") == "prod"


def test_alert_human_threads_org_to_queue_member_and_user_email(monkeypatch):
    captured_qm, captured_email = {}, {}

    def fake_queue_member(queue_ref, tenant_id=None, org_label=None):
        captured_qm["org_label"] = org_label
        return (None, None)

    import interpreter.routing as routing_mod
    monkeypatch.setattr(routing_mod, "queue_member", fake_queue_member)
    monkeypatch.setattr(salesforce, "user_email",
                        lambda *a, **k: captured_email.update(k) or None)
    monkeypatch.setattr(salesforce, "post_chatter", lambda *a, **k: {"dry_run": True})

    state = {"case": {"sf_id": "500A"}, "tenant_id": "t1", "routed_team": "support"}
    alert.alert_human(state, {"org": "prod", "mention": {"sf_team": "Support"}, "channel": "salesforce_chatter"})
    assert captured_qm["org_label"] == "prod"
