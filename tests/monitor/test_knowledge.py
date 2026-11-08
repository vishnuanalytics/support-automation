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
    ("@Sam Rao <[email]> @Mia Lee\n<[email]> Please check this", "internal_tag"),
    ("+Ravi Kumar <[email]> @Vik <[email]>\nPlease assist here.", "internal_tag"),
    ("Noted, checking\n\nBest regards\nAcmeEats", "ack"),
    ("<@U123> can you look", "internal_tag"),
    ("Hi Team,\n\nThe RIDs are priortized.", "ack"),
    ("The QC have been prioritized.", "ack"),
    ("Raised to our tech team as a bug, ticket 1234", None),
    ("Please share the order ID", None),
    ("Fixed. Please verify from your end and confirm the menu now shows correctly.", None),
])
def test_rule_kind(text, want):
    assert replies.rule_kind(text) == want


@pytest.mark.parametrize("text, want", [
    ("Hi Team,\n\nPlease ensure that the SOP is being followed. We are currently unable to\n"
     "locate the dump logs to provide the exact rejection reason.\n\nRegards,\nA", "no_answer"),
    ("We can't find the reason for this rejection.", "no_answer"),
    # Asks for something, so it's a request we can act on.
    ("Unable to locate the logs. Please request the team to repush the items.", None),
    ("We can't find this order, please share the order ID.", None),
    ("The logs show it was delivered.", None),
])
def test_a_reply_with_nothing_to_act_on(text, want):
    assert replies.rule_kind(text) == want


def test_a_rule_replaces_an_older_ai_label_on_the_same_text():
    text = "We are unable to locate the logs."
    case, saved = {}, {}

    class Store:
        def reply_labels(self, tenant_id, hashes):
            return {hashes[0]: {"kind": "needs_info", "summary": "Old"}}

        def save_reply_labels(self, tenant_id, labels):
            saved.update(labels)

    replies.label_latest("t", [(case, {"party": "third_party", "text": text})], Store(), {})
    assert case["reply"]["kind"] == "no_answer" and list(saved.values())[0]["method"] == "rule"


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


def test_strip_quoted_handles_wrapped_headers_and_footers():
    wrapped = "Fixed.\n\nOn Sat, Aug 8, 2026 at 6:57 PM 'Support' via tech <\na@b.co> wrote:\n\nold"
    assert knowledge.strip_quoted(wrapped) == "Fixed."
    assert knowledge.strip_quoted("Done.\n-- \nIMPORTANT NOTICE: secret") == "Done."
    assert knowledge.strip_quoted("Done.\n\nThis e-mail message may contain legal text") == "Done."


def test_core_text_removes_the_sign_off():
    assert replies.core_text("It is a scheduled order.\n\nBest Regards\nAcmeEats") == \
        "It is a scheduled order."


def test_a_polite_opening_is_not_treated_as_a_sign_off():
    text = "Hi Team,\nThank you for reaching out.\n\nThe item was updated at 8pm.\nIt is live now."
    assert "updated at 8pm" in replies.core_text(text)


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


def test_draft_prompt_puts_the_latest_message_first():
    timeline = [{"source": "Email", "party": "us", "party_label": "Agent", "at": "1", "text": "old"},
                {"source": "Email", "party": "third_party", "party_label": "Aggregator", "at": "2",
                 "text": "It is a scheduled order"},
                {"source": "Field change", "party": "system", "party_label": "System", "at": "3",
                 "text": "Status changed"}]
    user, _ = knowledge.draft_prompt({"subject": "S"}, "aggregator", timeline, [], None)
    head = user.split("Sources:")[0]
    assert "Aggregator: It is a scheduled order" in head and "Status changed" not in head


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


def test_a_failed_ai_call_gives_a_clear_message(monkeypatch):
    from interpreter.monitor import service

    def boom(*a, **k):
        raise RuntimeError("rate limit")

    monkeypatch.setattr(knowledge, "generate_draft", boom)
    monkeypatch.setattr(service, "case_detail", lambda *a, **k: {
        "llm_enabled": True, "case": {}, "timeline": [], "partner": None})
    got = service.ai_draft(None, "t", "1", "merchant")
    assert got["enabled"] and "busy" in got["error"]


def test_sorting_replies_uses_the_light_model():
    from interpreter import llm

    seen = {}

    def fake(system, user, **kw):
        seen.update(kw)
        return '{"kind": "update", "summary": ""}'

    replies.llm_kind("A real update on the case", "t", complete=fake)
    assert seen["model"] == llm.FAST_MODEL


def test_labelling_stops_at_its_time_limit_and_leaves_replies_as_real(monkeypatch):
    calls = []
    monkeypatch.setattr(replies, "llm_kind", lambda *a, **k: calls.append(1) or {"kind": "ack"})

    class Store:
        saved = {}

        def reply_labels(self, tenant_id, hashes):
            return {}

        def save_reply_labels(self, tenant_id, labels):
            Store.saved = labels

    long = "The team has reviewed this report and the details are in the attached summary note. " * 2
    rows = [({"id": str(i)}, {"party": "third_party", "text": f"{long}{i}"}) for i in range(3)]
    ticks = iter([0, 0, 100, 100, 100])
    replies.label_latest("t", rows, Store(), {"llm_enabled": True}, clock=lambda: next(ticks))
    assert len(calls) == 1 and sum("reply" in case for case, _ in rows) == 1


@pytest.mark.parametrize("text,want", [
    ("Thanks!", "thanks"),
    ("Thank you so much", "thanks"),
    ("ok", "thanks"),
    ("Images are visible now, thanks", "confirmed"),
    ("Its working now", "confirmed"),
    ("You can close this case", "confirmed"),
    # Hedged, questioning or mixed: left to the LLM, never guessed.
    ("Still not working", None),
    ("Is it fixed now?", None),
    ("Not visible now", None),
    ("It is working but prices are wrong", None),
    ("Thanks, but orders are still missing", None),
    ("Please share an update", None),
    ("", None),
    ("Hi", None),
    ("Hello team", None),
])
def test_customer_rule_kind(text, want):
    assert replies.customer_rule_kind(text) == want


class _LabelStore:
    def __init__(self, known=None):
        self.known, self.saved = known or {}, {}

    def reply_labels(self, tenant_id, hashes):
        return {h: v for h, v in self.known.items() if h in hashes}

    def save_reply_labels(self, tenant_id, labels):
        self.saved.update(labels)


def _merchant(text, at="2026-11-05T10:00:00Z"):
    return {"party": "customer", "text": text, "at": at}


def test_label_customer_attaches_the_label_and_when_it_was_said():
    case, store = {}, _LabelStore()
    replies.label_customer("t", [(case, _merchant("Its working now"))], store, {})
    assert case["customer_reply"] == {"kind": "confirmed", "summary": "", "at": "2026-11-05T10:00:00Z"}
    # Stored under its own hash, so it can't clash with a third-party label on the same text.
    assert list(store.saved) == [replies.customer_key("Its working now")]
    assert replies.customer_key("Its working now") != replies.text_hash("Its working now")


def test_label_customer_ignores_other_parties():
    case = {}
    replies.label_customer("t", [(case, {"party": "third_party", "text": "Its working now"})],
                           _LabelStore(), {})
    assert "customer_reply" not in case


def test_a_failed_customer_label_leaves_the_reply_needing_us(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(replies, "customer_llm_kind", boom)
    case = {}
    replies.label_customer("t", [(case, _merchant("The menu looks different on two outlets today."))],
                           _LabelStore(), {"llm_enabled": True})
    assert "customer_reply" not in case


def test_customer_llm_kind_redacts_and_defaults_to_needs_reply():
    from interpreter import llm

    seen = {}

    def fake(system, user, **kw):
        seen.update(kw, user=user)
        return '{"kind": "maybe", "summary": "Unclear"}'

    got = replies.customer_llm_kind("Call me on a@b.co", "t", complete=fake)
    assert got["kind"] == "needs_reply" and seen["model"] == llm.FAST_MODEL
    assert "a@b.co" not in seen["user"]


def test_customer_llm_is_used_only_when_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(replies, "customer_llm_kind",
                        lambda *a, **k: calls.append(1) or {"kind": "confirmed", "summary": "Fixed"})
    text = "Orders from both outlets are coming through properly again after the change."
    off, on = {}, {}
    replies.label_customer("t", [(off, _merchant(text))], _LabelStore(), {})
    replies.label_customer("t", [(on, _merchant(text))], _LabelStore(), {"llm_enabled": True})
    assert calls == [1] and "customer_reply" not in off and on["customer_reply"]["kind"] == "confirmed"


def test_a_stale_rule_label_is_dropped_when_the_rules_no_longer_give_it():
    store = _LabelStore({replies.customer_key("Hi"): {"kind": "thanks", "summary": "", "method": "rule"}})
    case = {}
    replies.label_customer("t", [(case, _merchant("Hi"))], store, {})
    assert "customer_reply" not in case


def test_an_llm_label_is_kept_when_no_rule_applies():
    text = "Orders from both outlets are coming through properly again after the change."
    store = _LabelStore({replies.customer_key(text): {"kind": "confirmed", "summary": "Fixed", "method": "llm"}})
    case = {}
    replies.label_customer("t", [(case, _merchant(text))], store, {})
    assert case["customer_reply"]["kind"] == "confirmed"
