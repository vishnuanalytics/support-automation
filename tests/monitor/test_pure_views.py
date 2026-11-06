from datetime import timedelta

from interpreter.monitor import brief, drafts, links, similar, timeline, verdict
from interpreter.monitor.service import is_hidden, sync_due

GH = "https://github.com/o/r/issues/1"


def case(**kw):
    return {"id": "1", "case_number": "42", "subject": "Orders stuck", "url": "https://sf/1",
            "waiting_raw": "Engineer", "waiting_party": "engineering", "links": [],
            "third_parties": [], **kw}


def test_timeline_merges_sources_in_order_with_labels(cfg):
    raw = {"emails": [{"from": "m@x", "subject": "s", "text": "t", "at": "2026-01-03T00:00:00Z",
                       "incoming": True}],
           "comments": [{"text": "c", "at": "2026-01-01T00:00:00.000+0000", "by": "Lead"}],
           "history": [{"field": "Status", "old": "New", "new": "In Progress",
                        "at": "2026-01-02T00:00:00Z", "by": "Lead"},
                       {"field": "Subject", "old": "a", "new": "b", "at": "2026-01-02T00:00:00Z"}]}
    ev = [{"kind": "github", "ok": True, "url": GH, "messages": [
        {"who": "dev", "ours": False, "at": "2026-01-04T00:00:00Z", "text": "x", "url": GH}]}]
    items = timeline.merge(case(), raw, ev, cfg)
    assert [i["source"] for i in items] == ["Case comment", "Field change", "Email", "GitHub"]
    assert items[2]["party_label"] == "Merchant" and items[3]["party_label"] == "Engineering"


def test_timeline_slack_party_follows_waiting_party(cfg):
    ev = [{"kind": "slack", "ok": True, "url": "u", "messages": [
        {"who": "x", "ours": False, "at": "2026-01-01T00:00:00Z", "text": "t"}]}]
    c = case(waiting_party="third_party")
    assert timeline.merge(c, None, ev, cfg)[0]["party_label"] == "Aggregator"


def test_timeline_undated_items_sort_last(cfg):
    raw = {"comments": [{"text": "none", "at": None}, {"text": "d", "at": "2026-01-01T00:00:00Z"}]}
    assert [i["text"] for i in timeline.merge(case(), raw, [], cfg)] == ["d", "none"]


def test_timeline_skips_unreadable_evidence(cfg):
    assert timeline.merge(case(), None, [{"kind": "github", "ok": False}], cfg) == []


POOL = [
    {"id": "a", "module": "Orders", "sub_module": "Relay", "third_parties": ["PartnerA"],
     "opened_at": "2026-01-01T00:00:00Z", "closed_at": "2026-01-02T00:00:00Z"},
    {"id": "b", "module": "Orders", "sub_module": "Relay", "third_parties": [],
     "opened_at": "2026-02-01T00:00:00Z", "closed_at": "2026-02-02T00:00:00Z"},
    {"id": "c", "module": "orders", "sub_module": "Other", "third_parties": [],
     "closed_at": "2026-03-01T00:00:00Z"},
    {"id": "d", "module": "Menu", "sub_module": "Relay", "third_parties": ["PartnerA"],
     "closed_at": "2026-04-01T00:00:00Z"},
]


def test_similar_ranks_by_match_count_then_recency():
    got = similar.rank(case(module="Orders", sub_module="Relay", third_parties=["partnera"]), POOL)
    assert [(r["id"], r["match_count"]) for r in got] == [("a", 3), ("b", 2), ("c", 1)]
    assert got[0]["hours_to_resolve"] == 24


def test_similar_needs_a_module():
    assert similar.rank(case(), POOL) == []


def test_similar_excludes_the_case_itself():
    assert "a" not in [r["id"] for r in similar.rank(case(id="a", module="Orders"), POOL)]


def test_duplicates_group_by_issue_across_url_spellings():
    a = case(id="a", links=links.parse(GH))
    b = case(id="b", links=links.parse("https://github.com/O/R/issues/1#issuecomment-5"))
    c = case(id="c", links=links.parse("https://github.com/o/r/issues/2"))
    groups = similar.group_duplicates([a, b, c])
    assert {k: [m["id"] for m in v] for k, v in groups.items()} == {"o/r#1": ["a", "b"]}


def test_a_case_with_one_issue_linked_twice_is_not_its_own_duplicate():
    c = case(links=links.parse(f"{GH}\nhttps://github.com/o/r/issues/1#x"))
    assert similar.group_duplicates([c]) == {}


def v(ball="us", **kw):
    return {"ball": ball, "mismatch": False, "next_action": "Do it", "stale_hours": 9.0, **kw}


def test_brief_only_shows_known_facts_and_cites_them(cfg):
    c = case(brand="Tasty", third_parties=["PartnerA"])
    b = brief.build(c, v(), None, [], cfg)
    assert [f["label"] for f in b["facts"]] == ["Brand", "Aggregator"]
    assert all(f["source"]["url"] == "https://sf/1" for f in b["facts"])
    assert b["problem"]["text"] == "Orders stuck" and b["next_action"] == "Do it"


def test_brief_waiting_since_uses_field_history(cfg):
    cfg = {**cfg, "fields": {**cfg["fields"], "waiting_on": "Waiting_Party__c"}}
    raw = {"history": [
        {"field": "Waiting_Party__c", "new": "Engineer", "at": "2026-01-01T00:00:00Z"},
        {"field": "Waiting_Party__c", "new": "Customer", "at": "2026-01-02T00:00:00Z"},
        {"field": "Waiting_Party__c", "new": "Engineer", "at": "2026-01-03T00:00:00Z"}]}
    assert brief.build(case(created_at="x"), v(), raw, [], cfg)["waiting"]["since"] == \
        "2026-01-03T00:00:00Z"


def test_brief_done_so_far_is_last_three_of_ours(cfg):
    raw = {"comments": [{"text": str(i), "at": f"2026-01-0{i}T00:00:00Z", "by": "L"}
                        for i in range(1, 6)]}
    items = timeline.merge(case(), raw, [], cfg)
    assert [d["text"] for d in brief.build(case(), v(), raw, items, cfg)["done_so_far"]] == \
        ["3", "4", "5"]


GH_EV = {"kind": "github", "ok": True, "url": GH, "label": "o/r#1", "messages": [
    {"who": "dev", "ours": False, "at": "2026-01-01T00:00:00Z", "text": "Fixed in 2.1", "url": GH}]}


def test_drafts_only_offered_when_facts_exist(cfg):
    assert [d["id"] for d in drafts.build(case(), v(), [], cfg)] == ["handoff_note"]


def test_customer_update_quotes_the_reply_and_needs_review(cfg):
    got = drafts.build(case(), v(), [GH_EV], cfg)
    assert got[0]["id"] == "customer_update" and got[0]["review_required"]
    assert "Fixed in 2.1" in got[0]["text"]


def test_every_draft_lists_sourced_facts(cfg):
    c = case(waiting_party="third_party", third_parties=["PartnerA"])
    for d in drafts.build(c, v(), [GH_EV], cfg):
        assert d["facts"] and all(f["source"]["label"] for f in d["facts"])


def test_suggested_draft_comes_first(cfg):
    assert drafts.build(case(), v("engineering"), [GH_EV], cfg)[0]["id"] == "github_nudge"


def test_hidden_until_new_activity(now):
    r = {"reviewed_at": now.isoformat()}
    older = {"latest_activity_at": (now - timedelta(hours=1)).isoformat()}
    newer = {"latest_activity_at": (now + timedelta(hours=1)).isoformat()}
    assert is_hidden(r, older, now) and not is_hidden(r, newer, now)


def test_snooze_expires_and_new_activity_ends_it(now):
    r = {"snooze_until": (now + timedelta(hours=2)).isoformat(), "snoozed_at": now.isoformat()}
    assert is_hidden(r, {"latest_activity_at": now.isoformat()}, now)
    assert not is_hidden(r, {"latest_activity_at": (now + timedelta(hours=1)).isoformat()}, now)
    assert not is_hidden(r, {}, now + timedelta(hours=3))


def test_sync_due_interval_and_daily(cfg, now):
    cfg = {**cfg, "sync_every_min": 60, "sync_hour": 9, "timezone": "UTC"}
    assert sync_due(cfg, None, now)
    assert not sync_due(cfg, now - timedelta(minutes=30), now)
    assert sync_due(cfg, now - timedelta(minutes=61), now)
    off = {**cfg, "sync_every_min": 0}
    assert not sync_due(off, now - timedelta(hours=2), now)
    assert sync_due(off, now - timedelta(hours=4), now)  # missed today's 09:00




def test_agent_nudge_needs_a_named_owner(cfg):
    got = {d["id"]: d for d in drafts.build(case(owner="Asha"), v(), [], cfg)}
    assert "Do it" in got["agent_nudge"]["text"] and "Asha" in got["agent_nudge"]["text"]


def test_inbound_email_is_labelled_by_sender_domain(cfg):
    cfg = {**cfg, "internal_email_domains": ["us.test"]}
    c = case(partner={"name": "AcmeEats", "email_domains": ["acmeeats.test"]})
    def label(sender):
        raw = {"emails": [{"from": sender, "text": "t", "at": "2026-01-01T00:00:00Z",
                           "incoming": True}]}
        return timeline.merge(c, raw, [], cfg)[0]["party_label"]
    assert label("ops@acmeeats.test") == "Aggregator"
    assert label("owner@gmail.com") == "Merchant"
    assert label("agent@us.test") == "Our team"


def test_related_dsp_email_appears_in_the_parent_timeline(cfg):
    ev = {"kind": "email", "ok": True, "related": True, "url": "e", "messages": [
        {"who": "ops@x.in", "ours": False, "at": "2026-01-01T00:00:00Z", "text": "t"}]}
    item = timeline.merge(case(), None, [ev], cfg)[0]
    assert item["source"] == "DSP email" and item["party_label"] == "Aggregator"


def test_raise_email_draft_has_recipients_and_comes_first(cfg):
    c = case(partner={"name": "AcmeEats", "channel": "email", "to_emails": ["a@x.test"],
                      "cc_emails": ["c@x.test"], "raise_template": "Title: {title}"})
    got = drafts.build(c, v("unknown", not_raised=True), [], cfg)
    assert got[0]["id"] == "raise_email" and got[0]["to"] == ["a@x.test"]
    assert got[0]["text"] == "Title: Orders stuck" and got[0]["review_required"]


def test_brief_shortens_long_messages(cfg):
    raw = {"comments": [{"text": "word " * 200, "at": "2026-01-01T00:00:00Z", "by": "L"}]}
    items = timeline.merge(case(), raw, [], cfg)
    assert len(brief.build(case(), v(), raw, items, cfg)["done_so_far"][0]["text"]) <= 141


def test_tidy_removes_footers_and_blank_lines(cfg):
    conf = {**cfg, "email_footer_markers": ["Not satisfied with"]}
    got = timeline.tidy("Hi,\n\n\n  Ticket: 1\n\nThanks\nNot satisfied with us? Click", conf)
    assert got == "Hi,\nTicket: 1\nThanks"


def test_email_subject_shows_only_when_it_adds_something(cfg):
    raw = {"emails": [
        {"subject": "Re: Orders stuck", "text": "a", "at": "2026-01-01T00:00:00Z"},
        {"subject": "FW: Refund", "text": "b", "at": "2026-01-02T00:00:00Z"}]}
    items = timeline.merge(case(), raw, [], cfg)
    assert items[0]["subject"] is None and items[1]["subject"] == "Refund"


def test_long_quiet_periods_read_in_days():
    assert verdict.age_text(234.6) == "10d" and verdict.age_text(11.5) == "11.5h"
