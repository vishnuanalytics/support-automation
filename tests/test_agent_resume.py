"""
Phase 20m — after `ask_human`, act on what the human did:
  * an agent's CaseComment -> bot polishes + sends a customer reply
  * an agent's outbound email -> just score the draft
  * nothing yet -> re-poll, then give up

Offline: a tiny in-memory fake Supabase client + monkeypatched Salesforce.
"""

from __future__ import annotations

import pytest

from api import worker
from interpreter import agent_reply


# ── fake supabase ────────────────────────────────────────────────────
class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, table):
        self.t = table
        self._filters = {}

    def select(self, *_a, **_k):
        return self

    def insert(self, row):
        row.setdefault("job_id", f"job-{len(self.t.rows)}")
        row.setdefault("run_id", f"run-{len(self.t.rows)}")
        self.t.rows.append(row)
        self.t.inserted.append(row)
        self._inserted = row
        return self

    def update(self, patch):
        self.t.updates.append(patch)
        self._patch = patch
        return self

    def delete(self):
        self._delete = True
        return self

    def eq(self, k, v):
        self._filters[k] = v
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        if getattr(self, "_inserted", None) is not None:
            return _Result([self._inserted])
        if getattr(self, "_patch", None) is not None:
            for r in self.t.rows:
                if all(r.get(k) == v for k, v in self._filters.items()):
                    r.update(self._patch)
            return _Result([])
        rows = [r for r in self.t.rows
                if all(r.get(k) == v for k, v in self._filters.items())]
        return _Result(rows)


class _Table:
    def __init__(self):
        self.rows = []
        self.inserted = []
        self.updates = []


class FakeSB:
    def __init__(self):
        self.tables = {"runs": _Table(), "jobs": _Table()}

    def table(self, name):
        return _Query(self.tables.setdefault(name, _Table()))


PARENT_RUN = "11111111-aaaa-4aaa-8aaa-111111111111"


def _seed_run(sb, **over):
    row = {
        "run_id": PARENT_RUN, "flow_id": "f0f0f0f0-0000-4000-8000-000000000000",
        "team": "email", "tenant_id": "00000000-0000-0000-0000-000000000000",
        "human_action": "pending", "created_at": "2026-08-31T07:40:00Z",
        "draft": "the bot's draft reply", "case_id": "500xxTEST",
        "case_payload": {"sf_id": "500xxTEST", "subject": "webhook help",
                         "body": "how do I test a webhook", "channel": "email",
                         "from": "cust@example.com"},
    }
    row.update(over)
    sb.tables["runs"].rows.append(row)
    return sb


@pytest.fixture(autouse=True)
def _no_mailbox(monkeypatch):
    monkeypatch.setattr("interpreter.mailbox.load_channel", lambda *_a, **_k: None)
    monkeypatch.setattr(worker.salesforce, "available", lambda: True)
    monkeypatch.setattr(worker, "_case_owned_by_user", lambda *_a, **_k: False)


def test_explicit_send_command_triggers_a_customer_reply(monkeypatch):
    sb = _seed_run(FakeSB())
    monkeypatch.setattr(worker.salesforce, "agent_response_since",
                        lambda *a, **k: {"guidance": "send this response to customer",
                                         "guidance_at": "x", "is_send_command": True,
                                         "outbound_email": None})
    monkeypatch.setattr(agent_reply, "resume_from_guidance",
                        lambda *a, **k: {"sent": True, "auto_sent": True, "via": "smtp",
                                         "reply": "Hi! POST a test payload to your webhook URL."})

    out = worker._check_resolution({"run_id": PARENT_RUN}, sb)

    assert out["human_action"] == "guided_resume"
    parent = next(r for r in sb.tables["runs"].rows if r["run_id"] == PARENT_RUN)
    assert parent["human_action"] == "guided_resume"
    resume = [r for r in sb.tables["runs"].inserted if r["source"] == "agent_resume"]
    assert len(resume) == 1
    assert resume[0]["outcome"] == "auto_reply"
    assert resume[0]["flow_id"] == parent["flow_id"] and resume[0]["team"] == "email"


def test_a_plain_investigation_note_does_not_email_the_customer(monkeypatch):
    sb = _seed_run(FakeSB())
    monkeypatch.setattr(worker.salesforce, "agent_response_since",
                        lambda *a, **k: {"guidance": "Checking with billing — will update here shortly.",
                                         "guidance_at": "x", "is_send_command": False,
                                         "outbound_email": None})
    monkeypatch.setattr(agent_reply, "resume_from_guidance",
                        lambda *a, **k: pytest.fail("a non-directive note must not send"))
    monkeypatch.setattr(worker, "_FEEDBACK_MAX_CHECKS", 3)

    out = worker._check_resolution({"run_id": PARENT_RUN, "checks": 0}, sb)

    assert out["waiting"] and out["note_seen"] is True
    parent = next(r for r in sb.tables["runs"].rows if r["run_id"] == PARENT_RUN)
    assert parent["human_action"] == "pending"                       # not resolved
    assert "Checking with billing" in (parent.get("human_reply") or "")
    assert not [r for r in sb.tables["runs"].inserted if r.get("source") == "agent_resume"]

    # ... and when the poll window ends it's `human_handling`, not `no_reply`
    last = worker._check_resolution({"run_id": PARENT_RUN, "checks": 2}, sb)
    assert last["human_action"] == "human_handling"


def test_agent_emailed_customer_directly_just_scores_the_draft(monkeypatch):
    sb = _seed_run(FakeSB())
    monkeypatch.setattr(worker.salesforce, "agent_response_since",
                        lambda *a, **k: {"guidance": None, "guidance_at": None,
                                         "outbound_email": "the bot's draft reply"})

    out = worker._check_resolution({"run_id": PARENT_RUN}, sb)

    assert out["human_action"] == "sent_as_is"          # identical text
    assert not [r for r in sb.tables["runs"].inserted if r.get("source") == "agent_resume"]


def test_nothing_yet_reschedules_then_gives_up(monkeypatch):
    sb = _seed_run(FakeSB())
    monkeypatch.setattr(worker.salesforce, "agent_response_since",
                        lambda *a, **k: {"guidance": None, "guidance_at": None, "outbound_email": None})
    monkeypatch.setattr(worker, "_FEEDBACK_MAX_CHECKS", 3)

    r1 = worker._check_resolution({"run_id": PARENT_RUN, "checks": 0}, sb)
    assert r1["waiting"] and r1["checks"] == 1
    assert any(j["kind"] == "check_resolution" for j in sb.tables["jobs"].inserted)

    r_last = worker._check_resolution({"run_id": PARENT_RUN, "checks": 2}, sb)
    assert r_last["human_action"] == "no_reply"


def test_already_resolved_is_a_noop(monkeypatch):
    sb = _seed_run(FakeSB(), human_action="edited")
    out = worker._check_resolution({"run_id": PARENT_RUN}, sb)
    assert "skipped" in out


# ── resume_from_guidance delivery ───────────────────────────────────
def test_resume_leaves_a_draft_when_autosend_is_off(monkeypatch):
    monkeypatch.setattr(agent_reply, "polish", lambda g, c, **k: "polished: " + g)
    seen = {}
    monkeypatch.setattr(agent_reply.salesforce, "add_case_comment",
                        lambda cid, body, **k: seen.update(cid=cid, body=body) or {"created": True})

    class Cfg:
        auto_send_enabled = False

    out = agent_reply.resume_from_guidance(
        {"sf_id": "500x", "subject": "s", "body": "b", "channel": "email"},
        "do X then Y", cfg=Cfg())

    assert out["sent"] is False and out["via"] == "case_comment_draft"
    assert "polished: do X then Y" in seen["body"]


def test_resume_sends_via_salesforce_when_no_mailbox(monkeypatch):
    monkeypatch.setattr(agent_reply, "polish", lambda g, c, **k: "polished reply")
    calls = {}
    monkeypatch.setattr(agent_reply.salesforce, "send_case_reply",
                        lambda cid, body, **k: calls.update(cid=cid, body=body, k=k) or
                        {"sent": True, "via": "email"})

    out = agent_reply.resume_from_guidance(
        {"sf_id": "500y", "subject": "s", "body": "b", "channel": "salesforce",
         "contact": {"email": "c@x.com"}},
        "the answer", cfg=None)

    assert out["auto_sent"] is True and out["via"] == "email"
    assert calls["cid"] == "500y" and calls["body"] == "polished reply"
    assert calls["k"].get("to_email") == "c@x.com"


# ── guidance applied on top of the bot's own draft ──────────────────
def test_bare_approval_sends_the_bot_draft_verbatim(monkeypatch):
    # no LLM call for a plain "send it" — the stored draft goes out as-is
    monkeypatch.setattr(agent_reply.llm, "complete",
                        lambda *a, **k: pytest.fail("approval must not call the LLM"))
    for note in ("send it", "Send this response to customer.", "LGTM", "approved"):
        assert agent_reply.polish(note, {"subject": "s", "body": "b"},
                                  draft="Hi Vishnu, here is the full answer.") == \
            "Hi Vishnu, here is the full answer."


def test_substantive_note_is_applied_to_the_draft(monkeypatch):
    got = {}
    monkeypatch.setattr(agent_reply.llm, "complete",
                        lambda *a, **k: got.update(k) or "final reply")
    out = agent_reply.polish("also mention the SLA is 24h",
                             {"subject": "s", "body": "b"}, draft="Hi, draft body.")
    assert out == "final reply"
    assert "draft body" in got["user"] and "SLA is 24h" in got["user"]


def test_check_resolution_reads_a_chatter_feedcomment_as_guidance(monkeypatch):
    """The bot @mentions on the Case *feed*, so a rep's reply is a FeedComment,
    not a CaseComment — agent_response_since must see both."""
    from interpreter import salesforce

    class _SF:
        def query(self, soql):
            if "FROM CaseComment" in soql:
                return {"records": [
                    {"CommentBody": "[bot draft — review before sending]\n\nx",
                     "CreatedDate": "2026-09-01T02:56:00.000+0000"}]}
            if "FROM FeedComment" in soql:
                return {"records": [
                    {"CommentBody": "<p>send this response to customer.</p>",
                     "CreatedDate": "2026-09-01T02:57:23.000+0000"}]}
            return {"records": []}

    monkeypatch.setattr(salesforce, "available", lambda: True)
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _SF())
    r = salesforce.agent_response_since("500X", "2026-09-01T02:55:00Z")
    assert r["guidance"] == "send this response to customer."   # the bot draft is skipped
    assert r["is_send_command"] is True


# ── explicit-directive detection ────────────────────────────────────
def test_looks_like_send_command():
    from interpreter import salesforce
    for yes in ("send", "send it", "Send it.", "SEND THIS TO THE CUSTOMER",
                "send: also mention the refund window is 30 days", "lgtm",
                "approved", "reply:", "send this response to customer"):
        assert salesforce.looks_like_send_command(yes), yes
    for no in ("Checking with billing, will update shortly",
               "I think this is a known issue with SSO", "assigning to me",
               "customer called, wants a callback", "sent them a Slack ping",
               "please investigate the logs first"):
        assert not salesforce.looks_like_send_command(no), no


# ── Phase 23h: the "Send Bot Draft to Customer" quick action ────────
def test_send_bot_draft_quick_action_sends_and_disarms(monkeypatch):
    sb = _seed_run(FakeSB())
    monkeypatch.setattr(agent_reply, "resume_from_guidance",
                        lambda *a, **k: {"sent": True, "auto_sent": True, "via": "smtp",
                                         "reply": "the sent reply"})

    class _Case:
        @staticmethod
        def get(_cid):
            return {"Bot_Send_Note__c": ""}

    class _SF:
        Case = _Case()

    monkeypatch.setattr(worker.salesforce, "client_for", lambda *a, **k: _SF())
    cleared = {}
    monkeypatch.setattr(worker.salesforce, "update_case_fields",
                        lambda cid, fields, **k: cleared.update(cid=cid, **fields))

    out = worker._send_bot_draft("500xxTEST", sb)

    assert out["sent"] is True and out["run_id"] == PARENT_RUN
    parent = next(r for r in sb.tables["runs"].rows if r["run_id"] == PARENT_RUN)
    assert parent["human_action"] == "guided_resume"
    assert cleared["Bot_Send_Draft__c"] is False and cleared["Bot_Send_Note__c"] == ""
    qa = [r for r in sb.tables["runs"].inserted if r.get("source") == "quick_action"]
    assert len(qa) == 1 and qa[0]["outcome"] == "auto_reply"


def test_send_bot_draft_with_no_run_just_disarms(monkeypatch):
    sb = FakeSB()   # no run seeded
    cleared = {}
    monkeypatch.setattr(worker.salesforce, "update_case_fields",
                        lambda cid, fields, **k: cleared.update(fields))
    out = worker._send_bot_draft("500NOPE", sb)
    assert out["skipped"] == "no run for this case"
    assert cleared["Bot_Send_Draft__c"] is False


def test_send_bot_draft_is_idempotent_after_send(monkeypatch):
    sb = _seed_run(FakeSB(), human_action="guided_resume")
    monkeypatch.setattr(agent_reply, "resume_from_guidance",
                        lambda *a, **k: pytest.fail("must not re-send an already-sent draft"))
    monkeypatch.setattr(worker.salesforce, "update_case_fields", lambda *a, **k: None)
    out = worker._send_bot_draft("500xxTEST", sb)
    assert out["skipped"] == "draft already sent"
