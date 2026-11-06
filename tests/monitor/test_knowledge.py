import json

import pytest

from interpreter.monitor import knowledge, replies
from interpreter.monitor.config import merge

CFG = merge({"third_party_label": "Aggregator", "customer_label": "Merchant",
             "internal_email_domains": ["us.test"]})


@pytest.mark.parametrize("text,want", [
    ("Noted, let me check", "ack"),
    ("Thanks team, checking", "ack"),
    ("Hi, we're looking into this", "ack"),
    ("", "ack"),
    ("@sam please check this one", "internal_tag"),
    ("<@U123> can you look", "internal_tag"),
    ("Raised to our tech team as a bug, ticket 1234", None),
    ("Please share the order ID", None),
    ("Fixed. Please verify from your end and confirm the menu now shows correctly.", None),
])
def test_rule_kind(text, want):
    assert replies.rule_kind(text) == want


def test_llm_kind_redacts_and_validates():
    sent = {}

    def fake(system, user, **kw):
        sent["user"] = user
        return json.dumps({"kind": "needs_info", "summary": "Needs order ID"})

    got = replies.llm_kind("mail me at a@b.co", "t", complete=fake)
    assert got["kind"] == "needs_info" and "a@b.co" not in sent["user"]


def test_llm_unknown_kind_counts_as_a_real_update():
    got = replies.llm_kind("x", "t", complete=lambda *a, **k: '{"kind": "maybe"}')
    assert got["kind"] == "update"


def test_strip_quoted_keeps_only_the_new_text():
    text = "Fixed now.\n\nOn Mon, 3 Nov 2026 at 10:00, Ops <a@b.co> wrote:\n> old text"
    assert knowledge.strip_quoted(text) == "Fixed now."


def test_strip_quoted_leaves_a_message_that_starts_with_a_quote_header():
    assert "wrote" in knowledge.strip_quoted("On Mon, 3 Nov 2026 Ops wrote:\nbody")


def test_conversation_is_ordered_labelled_and_redacted():
    case = {"subject": "Menu sync failed", "third_parties": ["AcmeEats"], "module": "Menu",
            "partner": {"name": "AcmeEats", "email_domains": ["acme.test"]},
            "description": "Call +91 98765 43210",
            "messages": [
                {"from": "ops@acme.test", "incoming": True, "at": "2026-09-02T00:00:00Z",
                 "text": "Fixed on our side"},
                {"from": "agent@us.test", "incoming": False, "at": "2026-09-01T00:00:00Z",
                 "text": "Please check a@b.co"}],
            "comments": ["Merchant confirmed"]}
    md = knowledge.conversation_md(case, CFG)
    assert md.index("### Agent") < md.index("### Aggregator")
    assert "a@b.co" not in md and "98765" not in md and "Merchant confirmed" in md


def test_entry_id_is_stable_so_reruns_update_the_same_entry():
    assert knowledge.entry_id("500abc") == knowledge.entry_id("500abc")


def test_draft_prompt_redacts_and_numbers_sources():
    timeline = [{"source": "Email", "party_label": "Merchant", "at": "2026-11-01", "text": "mail a@b.co",
                 "url": "u"}]
    similar = [{"text": "Past fix", "url": "kb://1"}]
    user, sources = knowledge.draft_prompt({"subject": "S"}, "merchant", timeline, similar, None)
    assert "a@b.co" not in user and "[2] Past case" in user and len(sources) == 2


def test_generate_draft_returns_sources(monkeypatch):
    monkeypatch.setattr(knowledge, "similar_context", lambda *a, **k: [])
    timeline = [{"source": "Email", "party_label": "Merchant", "at": "x", "text": "hi", "url": "u"}]
    fake = lambda *a, **k: json.dumps({"situation": "s", "draft": "d", "missing": ["ID"]})  # noqa: E731
    got = knowledge.generate_draft("t", {"subject": "S"}, "aggregator", timeline, None, None, fake)
    assert got["draft"] == "d" and got["missing"] == ["ID"] and got["sources"][0]["n"] == 1


def test_source_markers_are_removed_from_the_draft_but_placeholders_stay(monkeypatch):
    monkeypatch.setattr(knowledge, "similar_context", lambda *a, **k: [])
    timeline = [{"source": "Email", "party_label": "Merchant", "at": "x", "text": "hi", "url": "u"}]
    fake = lambda *a, **k: json.dumps({"draft": "Logs confirm this [2][3]. Reach us at [email]."})  # noqa: E731
    got = knowledge.generate_draft("t", {"subject": "S"}, "merchant", timeline, None, None, fake)
    assert got["draft"] == "Logs confirm this. Reach us at [email]."
