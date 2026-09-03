"""KIL-a — Case lifecycle -> Neo4j graph sync. Offline: no Salesforce, no
Neo4j; the driver is captured through a fake."""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ingestion import case_graph_sync as cgs
from interpreter import case_memory

_CASE = {
    "Id": "500XX0000000abcAAA",
    "CaseNumber": "00001188",
    "Subject": "Webhook 500s since this morning",
    "Description": "Our webhook has been returning 500 since 09:00. Ref 88123456.",
    "Status": "Escalated",
    "Type": "Problem",
    "Origin": "Email",
    "IsClosed": False,
    "CreatedDate": "2026-08-30T09:00:00.000+0000",
    "ClosedDate": None,
    "LastModifiedDate": "2026-08-31T12:00:00.000+0000",
    "AccountId": "001XX000003DfgAAA",
    "ContactId": "003XX000004TmiAAA",
    "Contact": {"Email": "rose@edge.com"},
    "Module__c": "API & Webhooks",
    "Routed_Team__c": "tier2",
    "Account": {"Tier__c": "premium"},
    "CaseComments": {"records": [
        {"Id": "00aXX01", "CommentBody": "Confirmed reproducible on staging.",
         "CreatedById": "005AGENT", "CreatedDate": "2026-08-31T10:00:00.000+0000",
         "IsPublished": True},
    ]},
    "EmailMessages": {"records": [
        {"Id": "02sIN", "Incoming": True, "FromAddress": "rose@edge.com",
         "TextBody": "Still broken, any update?", "MessageDate": "2026-08-31T09:30:00.000+0000",
         "CreatedById": "005SYS"},
        {"Id": "02sOUT", "Incoming": False, "FromAddress": "support@acme.com",
         "TextBody": "We've reproduced it and escalated to engineering.",
         "MessageDate": "2026-08-31T11:00:00.000+0000", "CreatedById": "005AGENT"},
    ]},
}


def test_case_row_normalises_fields():
    r = cgs._case_row(_CASE)
    assert r["sf_id"] == "500XX0000000abcAAA"
    assert r["case_number"] == "00001188"
    assert r["status"] == "Escalated" and r["is_closed"] is False
    assert r["tier"] == "premium" and r["routed_team"] == "tier2"
    assert r["module"] == "API & Webhooks" and r["case_type"] == "Problem"
    assert r["account_id"] == "001XX000003DfgAAA"


def test_messages_roles_authors_and_order():
    msgs = cgs._messages(_CASE)
    ids = [m["id"] for m in msgs]
    # description first (09:00), then inbound email (09:30), comment (10:00), outbound (11:00)
    assert ids == ["500XX0000000abcAAA:desc", "02sIN", "00aXX01", "02sOUT"]
    by_id = {m["id"]: m for m in msgs}
    assert by_id["500XX0000000abcAAA:desc"]["role"] == "inbound"
    assert by_id["500XX0000000abcAAA:desc"]["author_kind"] == "customer"
    assert by_id["02sIN"]["role"] == "inbound" and by_id["02sIN"]["author_kind"] == "customer"
    assert by_id["00aXX01"]["role"] == "agent_note" and by_id["00aXX01"]["author_kind"] == "agent"
    assert by_id["02sOUT"]["role"] == "agent_reply" and by_id["02sOUT"]["author_kind"] == "agent"


def test_bot_draft_comment_is_tagged_draft_not_agent_note():
    case = {**_CASE, "CaseComments": {"records": [
        {"Id": "d1", "CommentBody": "[bot draft — review before sending]\n\nHi there, try re-authing.",
         "CreatedById": "005INT", "CreatedDate": "2026-08-31T10:05:00.000+0000"},
        {"Id": "n1", "CommentBody": "Escalating to eng, repro attached.",
         "CreatedById": "005AGENT", "CreatedDate": "2026-08-31T10:06:00.000+0000"},
    ]}}
    by_id = {m["id"]: m for m in cgs._messages(case)}
    assert by_id["d1"]["role"] == "draft" and by_id["d1"]["author_kind"] == "bot"
    assert by_id["n1"]["role"] == "agent_note" and by_id["n1"]["author_kind"] == "agent"


def test_messages_redacts_customer_identifiers():
    body = next(m for m in cgs._messages(_CASE) if m["id"].endswith(":desc"))["text"]
    assert "88123456" not in body and "<num>" in body


def test_messages_skips_empty_bodies():
    case = {**_CASE, "Description": "", "CaseComments": {"records": []},
            "EmailMessages": {"records": [
                {"Id": "e1", "Incoming": True, "TextBody": "   ", "MessageDate": "x"}]}}
    assert cgs._messages(case) == []


def test_sync_case_lifecycle_builds_the_expected_cypher_params(monkeypatch):
    captured = {}

    class _FakeDriver:
        def execute_query(self, cypher, **params):
            captured["cypher"] = cypher
            captured["params"] = params

        def close(self):
            pass

    monkeypatch.setattr(case_memory, "_driver_or_none", lambda: _FakeDriver())
    ok = case_memory.sync_case_lifecycle(cgs._case_row(_CASE), cgs._messages(_CASE))
    assert ok is True
    p = captured["params"]
    assert p["sf_id"] == "500XX0000000abcAAA"
    assert p["is_closed"] is False and p["tenant_id"]  # str, non-empty
    assert p["module"] == "API & Webhooks" and p["account_id"] == "001XX000003DfgAAA"
    assert len(p["messages"]) == 4
    assert {m["role"] for m in p["messages"]} == {"inbound", "agent_note", "agent_reply"}
    # security fix (2026-09-03) -- Case/Account/Message MERGE keys include
    # tenant_id, not just the Salesforce-org-scoped id, so two tenants can
    # never collide into the same node (SF record ids aren't globally unique).
    assert "MERGE (c:Case {sf_id: $sf_id, tenant_id: $tenant_id})" in captured["cypher"]
    assert "MERGE (a:Account {sf_id: $account_id, tenant_id: $tenant_id})" in captured["cypher"]
    assert "MERGE (mm:Message {id: msg.id, tenant_id: $tenant_id})" in captured["cypher"]
    assert "MERGE (c)-[:HAS_MESSAGE]->(mm)" in captured["cypher"]


def test_sync_case_lifecycle_no_driver_is_a_soft_false(monkeypatch):
    monkeypatch.setattr(case_memory, "_driver_or_none", lambda: None)
    assert case_memory.sync_case_lifecycle({"sf_id": "x"}, []) is False


def test_sync_dry_run_needs_no_neo4j(monkeypatch):
    monkeypatch.setattr(cgs, "_TENANT", "00000000-0000-0000-0000-000000000000")

    class _SF:
        def query(self, soql):
            return {"records": [_CASE]}

    from interpreter import salesforce
    monkeypatch.setattr(salesforce, "available", lambda: True)
    monkeypatch.setattr(salesforce, "client_for", lambda _t: _SF())
    monkeypatch.setattr(cgs, "get_supabase", lambda: pytest.fail("dry-run must not touch Supabase"))
    # constraint-ensure is wrapped in try/except; force it to be a no-op
    monkeypatch.setattr("ingestion.neo4j_sync.get_neo4j_driver",
                        lambda: (_ for _ in ()).throw(RuntimeError("no neo4j")))
    rc = cgs.sync(since="1970-01-01", limit=10, one_id=None, dry=True)
    assert rc == 0
