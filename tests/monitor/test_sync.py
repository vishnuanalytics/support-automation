from copy import deepcopy
from datetime import timedelta

import pytest
import requests

from interpreter.monitor import demo, readers, sync, verdict
from interpreter.monitor.config import merge

GH = "https://github.com/o/r/issues/1"


class FakeStore:
    def __init__(self):
        self.prior, self.saved, self.events, self.closed, self.resolved = {}, [], [], [], []
        self.finished = None
        self.locked = False
        self.ai = {}

    def start_sync(self, tenant_id, trigger):
        return None if self.locked else "s1"

    def finish_sync(self, sync_id, status, counts, errors):
        self.finished = (status, counts, errors)

    def ai_feed(self, tenant_id):
        return self.ai

    def open_cases(self, tenant_id):
        return self.prior

    def save_cases(self, tenant_id, rows, now):
        self.saved = rows

    def save_evidence(self, tenant_id, evidence, now):
        self.evidence = evidence

    def close_cases(self, tenant_id, ids):
        self.closed = ids

    issue_rows: dict = {}
    issues_saved: dict = {}
    issues_closed: list = []

    def issue_reads(self, tenant_id):
        return self.issue_rows

    def save_issues(self, tenant_id, reads, now):
        self.issues_saved = reads

    def close_issues(self, tenant_id, urls):
        self.issues_closed = urls

    closed_saved: list = []

    def save_closed_issues(self, tenant_id, rows):
        self.closed_saved = rows

    imported: dict = {}
    partner_rows: list = []

    def partners(self, tenant_id):
        return self.partner_rows

    labels: dict = {}

    def reply_labels(self, tenant_id, hashes):
        return {k: v for k, v in self.labels.items() if k in hashes}

    def save_reply_labels(self, tenant_id, labels):
        self.saved_labels = labels

    def imported_evidence(self, tenant_id):
        return self.imported

    def add_events(self, tenant_id, sync_id, events):
        self.events = events

    def last_resolved_at(self, tenant_id):
        return None

    def save_resolved(self, tenant_id, rows):
        self.resolved = rows


class FakeReader:
    def __init__(self, cases):
        self.cases = cases
        self.timeline_calls = []

    def list_dependency_cases(self):
        return deepcopy(self.cases)

    def get_cases(self, ids):
        return []

    def children_of(self, ids):
        return {}

    def brand_stats(self, brands):
        return {}

    def post_links(self, ids):
        self.post_ids = ids
        return self.posts

    posts: dict = {}

    def latest_emails(self, ids):
        self.email_ids = ids
        return self.emails

    emails: dict = {}

    def timeline(self, case_id):
        self.timeline_calls.append(case_id)
        if case_id == "bad":
            raise RuntimeError("boom")
        return {"emails": [], "comments": [], "history": []}

    def resolved_since(self, since):
        return [{"id": "r1"}]


class ListsNothing:
    def open_issue_links(self, owner, repo, label):
        return []

    def closed_issues(self, owner, repo, label, since):
        return []


class OkGithub(ListsNothing):
    calls = 0

    def fetch(self, link):
        OkGithub.calls += 1
        return {"kind": "github", "url": link["url"], "ok": True, "state": "open", "messages": []}


class ExplodingGithub(ListsNothing):
    def fetch(self, link):
        raise requests.ConnectionError("down")


def case(cid, url=GH, **kw):
    from interpreter.monitor.links import parse
    return {"id": cid, "case_number": cid, "source": "salesforce", "waiting_party": "engineering",
            "waiting_raw": "Engineer", "links": parse(url), "third_parties": [],
            "last_modified_at": "2026-11-05T11:00:00Z", **kw}


def run(store, reader, evidence=None, cfg=None, now=None):
    from datetime import datetime, timezone
    now = now or datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    return sync.run("t", store, reader, evidence or {"github": OkGithub()}, cfg or merge({}),
                    now=now)


def test_saves_verdicts_and_counts():
    store = FakeStore()
    result = run(store, FakeReader([case("1"), case("2", url=None)]))
    assert result["status"] == "ok" and result["total"] == 2
    assert result["missing_link"] == 1
    assert store.resolved == [{"id": "r1"}]


def test_bad_link_does_not_fail_the_sync():
    store = FakeStore()
    result = run(store, FakeReader([case("1")]), {"github": ExplodingGithub()})
    assert result["status"] == "partial" and result["total"] == 1
    assert "could not read github" in result["errors"][0]["error"]
    assert result["errors"][0]["case_number"] == "1"


def test_bad_timeline_does_not_fail_the_sync():
    result = run(FakeStore(), FakeReader([case("bad"), case("2")]))
    assert result["status"] == "partial" and result["total"] == 2


def test_listing_failure_changes_nothing():
    class Broken(FakeReader):
        def list_dependency_cases(self):
            raise RuntimeError("session expired sekret-token")

    store = FakeStore()
    store.prior = {"1": {"case": {}, "verdict": {}}}
    result = run(store, Broken([]))
    assert result["status"] == "failed" and store.closed == [] and store.saved == []
    assert "sekret" not in result["error"]


def test_lock_skips_overlapping_sync():
    store = FakeStore()
    store.locked = True
    assert "skipped" in run(store, FakeReader([]))


def test_same_url_is_read_once_per_sync():
    OkGithub.calls = 0
    run(FakeStore(), FakeReader([case("1"), case("2")]))
    assert OkGithub.calls == 1


def test_only_changed_cases_refetch_timelines():
    store = FakeStore()
    store.prior = {"1": {"case": {"last_modified_at": "2026-11-05T11:00:00Z"},
                         "verdict": {}, "timeline": {"emails": []}}}
    reader = FakeReader([case("1"), case("2")])
    run(store, reader)
    assert reader.timeline_calls == ["2"]


def test_gone_cases_are_soft_closed():
    store = FakeStore()
    store.prior = {"old": {"case": {}, "verdict": {}, "timeline": None}}
    run(store, FakeReader([case("1")]))
    assert store.closed == ["old"]


def test_ai_handoff_joins_the_queue_without_a_case_record():
    store = FakeStore()
    store.ai = {"run-case": {"state": "handed_off", "outcome": "handover", "subject": "Refund",
                             "url": "/runs/1"}}
    result = run(store, FakeReader([]))
    assert result["my_turn"] == 1 and store.saved[0]["case"]["source"] == "ai"


def dsp_cfg():
    return merge({"dependency_record_type": "DSP"})


def agg(cid, **kw):
    return case(cid, url=None, waiting_party="third_party", waiting_raw="Aggregator", **kw)


EMAIL = {"who": "ops@partnera.in", "ours": False, "at": "2026-11-05T10:00:00Z", "text": "Need IDs"}


def test_dsp_case_reply_by_email_is_our_turn():
    reader = FakeReader([agg("1", record_type="DSP")])
    reader.emails = {"1": [EMAIL]}
    store = FakeStore()
    result = run(store, reader, cfg=dsp_cfg())
    assert result["my_turn"] == 1 and result["missing_link"] == 0


def test_support_case_reads_its_child_dsp_emails():
    class WithChild(FakeReader):
        def children_of(self, ids):
            return {"1": [{"id": "d", "record_type": "DSP", "links": []}]}

    reader = WithChild([agg("1", record_type="Support")])
    reader.emails = {"d": [{**EMAIL, "ours": True}]}
    store = FakeStore()
    run(store, reader, cfg=dsp_cfg())
    assert "d" in reader.email_ids
    assert store.saved[0]["verdict"]["ball"] == "third_party"
    assert store.saved[0]["case"]["email_evidence"]["related"]


def test_support_case_emails_are_not_third_party_evidence():
    reader = FakeReader([agg("1", record_type="Support")])
    reader.emails = {"1": [EMAIL]}
    store = FakeStore()
    result = run(store, reader, cfg=dsp_cfg())
    assert result["not_raised"] == 1 and result["my_turn"] == 0


def test_child_links_count_for_the_parent():
    class WithChild(FakeReader):
        def children_of(self, ids):
            from interpreter.monitor.links import parse
            return {"1": [{"id": "d", "record_type": "DSP", "links": parse(GH)}]}

    store = FakeStore()
    result = run(store, WithChild([agg("1")]), cfg=dsp_cfg())
    assert result["missing_link"] == 0
    assert "links" not in store.saved[0]["case"]["children"][0]


SLACK_URL = "https://x.slack.com/archives/C1/p1700000000000100"


class NoLoginSlack:
    def fetch(self, link):
        return {"kind": "slack", "url": link["url"], "ok": False,
                "error": "connect Slack in Case Monitor to read this"}


def test_sync_keeps_an_imported_thread_instead_of_an_error():
    store = FakeStore()
    store.imported = {SLACK_URL: {
        "kind": "slack", "url": SLACK_URL, "ok": True, "imported_at": "2026-11-05T00:00:00Z",
        "messages": [{"who": "ops", "ours": False, "at": "2026-11-05T10:00:00Z", "text": "x"}]}}
    c = case("1", url=SLACK_URL, waiting_party="third_party", waiting_raw="Aggregator")
    result = run(store, FakeReader([c]), {"slack": NoLoginSlack()})
    assert result["status"] == "ok" and result["my_turn"] == 1
    assert store.evidence[0]["imported_at"]


def test_a_real_read_replaces_an_imported_thread():
    class Works:
        def fetch(self, link):
            return {"kind": "slack", "url": link["url"], "ok": True, "messages": []}

    store = FakeStore()
    store.imported = {SLACK_URL: {"url": SLACK_URL, "ok": True, "imported_at": "x"}}
    run(store, FakeReader([case("1", url=SLACK_URL)]), {"slack": Works()})
    assert "imported_at" not in store.evidence[0]


class ImportTable:
    """The few supabase-client calls the import path makes."""

    def __init__(self, rows):
        self.rows, self.saved = rows, []

    def table(self, name):
        return self

    def __getattr__(self, name):
        return lambda *a, **k: self

    def upsert(self, payload):
        self.saved += payload
        return self

    def execute(self):
        return type("R", (), {"data": self.rows})


def _case_row():
    from interpreter.monitor.links import parse
    return [{"case_data": {"case_number": "7", "links": parse(SLACK_URL)}}]


def test_import_cleans_text_and_records_when_it_was_read():
    from interpreter.monitor import service

    sb = ImportTable(_case_row())
    service.import_slack_threads(sb, "t", [{"url": SLACK_URL, "messages": [
        {"who": "ops", "ours": False, "at": "2026-11-05T10:00:00Z",
         "text": "see https://s3.io/a?Signature=sek " + "y" * 600}]}])
    data = sb.saved[0]["data"]
    assert data["imported_at"] and "sek" not in data["messages"][0]["text"]
    assert len(data["messages"][0]["text"]) <= 401


def test_import_rejects_a_thread_not_linked_to_a_case():
    from interpreter.monitor import service

    with pytest.raises(ValueError):
        service.import_slack_threads(ImportTable(_case_row()), "t",
                                     [{"url": "https://x.slack.com/archives/C9/p1", "messages": []}])


def test_import_records_a_thread_the_person_cant_see():
    from interpreter.monitor import service

    sb = ImportTable(_case_row())
    service.import_slack_threads(sb, "t", [{"url": SLACK_URL, "error": "channel not visible"}])
    assert not sb.saved[0]["data"]["ok"] and sb.saved[0]["data"]["error"]


def test_empty_link_field_falls_back_to_case_posts():
    from interpreter.monitor.links import parse

    reader = FakeReader([case("1", url=None), case("2")])
    reader.posts = {"1": [{**parse(GH)[0], "via": "post"}]}
    store = FakeStore()
    result = run(store, reader)
    assert reader.post_ids == ["1"] and result["missing_link"] == 0
    assert store.saved[0]["case"]["links"][0]["via"] == "post"


def test_post_read_failure_does_not_fail_the_sync():
    class Broken(FakeReader):
        def post_links(self, ids):
            raise RuntimeError("boom")

    result = run(FakeStore(), Broken([case("1", url=None)]))
    assert result["status"] == "partial" and result["total"] == 1


def test_posts_only_yield_ticketing_and_chat_links():
    jira = "https://x.atlassian.net/browse/A-1"
    sf = FakeSF([{"ParentId": "1", "Body": f"<p><a href=\"{jira}\">t</a> https://x.io/a.png</p>",
                  "LinkUrl": None,
                  "FeedComments": {"records": [{"CommentBody": f"{GH}?a=1&amp;b=2"}]}}])
    got = readers.SalesforceCaseReader(sf, CFG).post_links(["1"])["1"]
    assert sorted(x["kind"] for x in got) == ["github", "jira"]
    assert all(x["via"] == "post" for x in got)


ACME = {"name": "AcmeEats", "channel": "email", "to_emails": ["a@acmeeats.test"],
        "cc_emails": ["cc@x.test"], "email_domains": ["acmeeats.test"]}


def test_email_channel_case_needs_no_link_once_emailed():
    reader = FakeReader([agg("1", record_type="DSP", third_parties=["acmeeats"])])
    reader.emails = {"1": [{**EMAIL, "ours": True}]}
    store = FakeStore()
    store.partner_rows = [ACME]
    result = run(store, reader, cfg=dsp_cfg())
    assert result["missing_link"] == 0 and result["not_raised"] == 0
    assert store.saved[0]["verdict"]["ball"] == "third_party"


def test_email_channel_with_no_email_is_not_raised_not_missing_link():
    store = FakeStore()
    store.partner_rows = [ACME]
    result = run(store, FakeReader([agg("1", record_type="DSP", third_parties=["AcmeEats"])]),
                 cfg=dsp_cfg())
    assert result["not_raised"] == 1 and result["missing_link"] == 0
    assert "by email" in store.saved[0]["verdict"]["next_action"]


def test_aggregator_case_without_a_dsp_case_is_flagged():
    store = FakeStore()
    store.partner_rows = [ACME]
    run(store, FakeReader([agg("1", record_type="Support", third_parties=["AcmeEats"])]),
        cfg=dsp_cfg())
    verdict_ = store.saved[0]["verdict"]
    assert verdict_["not_raised"] and "Raise a DSP case first" in verdict_["next_action"]


def test_slack_channel_still_needs_a_link():
    store = FakeStore()
    store.partner_rows = [{"name": "ChatCo", "channel": "slack"}]
    result = run(store, FakeReader([agg("1", record_type="DSP", third_parties=["ChatCo"])]),
                 cfg=dsp_cfg())
    assert result["missing_link"] == 1 and "on Slack" in store.saved[0]["verdict"]["next_action"]


def test_whatsapp_channel_is_checked_by_hand():
    store = FakeStore()
    store.partner_rows = [{"name": "WaCo", "channel": "whatsapp"}]
    result = run(store, FakeReader([agg("1", record_type="DSP", third_parties=["WaCo"])]),
                 cfg=dsp_cfg())
    assert result["missing_link"] == 0 and result["not_raised"] == 0
    assert "by hand" in store.saved[0]["verdict"]["next_action"]


def test_support_case_keeps_only_emails_with_the_partner():
    reader = FakeReader([agg("1", record_type="Support", third_parties=["AcmeEats"])])
    reader.emails = {"1": [
        {"who": "owner@shop.test", "to": "us@x.test", "ours": False, "at": "2026-11-05T09:00:00Z"},
        {"who": "us@x.test", "to": "a@acmeeats.test, b@x.test", "ours": True,
         "at": "2026-11-05T10:00:00Z"}]}
    store = FakeStore()
    store.partner_rows = [ACME]
    run(store, reader, cfg=dsp_cfg())
    assert len(store.saved[0]["case"]["email_evidence"]["messages"]) == 1
    assert store.saved[0]["verdict"]["ball"] == "third_party"


def test_alias_matches_the_directory_entry():
    store = FakeStore()
    store.partner_rows = [{**ACME, "aliases": ["AcmeFood"]}]
    run(store, FakeReader([agg("1", record_type="DSP", third_parties=["AcmeFood"])]), cfg=dsp_cfg())
    assert store.saved[0]["case"]["partner"]["name"] == "AcmeEats"


def dsp_parent(**kw):
    return {"id": "p", "case_number": "9", "record_type": "DSP", "status": "In Progress", **kw}


def test_support_case_uses_its_parent_dsp():
    reader = FakeReader([agg("1", record_type="Support", third_parties=["AcmeEats"],
                             parent=dsp_parent())])
    reader.emails = {"p": [{**EMAIL, "ours": True}]}
    store = FakeStore()
    store.partner_rows = [ACME]
    result = run(store, reader, cfg=dsp_cfg())
    assert result["not_raised"] == 0 and store.saved[0]["case"]["has_dsp"]
    assert store.saved[0]["verdict"]["ball"] == "third_party"
    assert store.saved[0]["case"]["email_evidence"]["related"]


def test_a_resolved_parent_dsp_is_our_turn():
    c = agg("1", record_type="Support", parent=dsp_parent(status="Resolved", closed=True))
    store = FakeStore()
    result = run(store, FakeReader([c]), cfg=dsp_cfg())
    assert result["my_turn"] == 1
    assert "Update the customer" in store.saved[0]["verdict"]["next_action"]


def test_parent_posts_count_for_an_empty_support_case():
    from interpreter.monitor.links import parse

    reader = FakeReader([agg("1", record_type="Support", parent=dsp_parent())])
    reader.posts = {"p": [{**parse(GH)[0], "via": "post"}]}
    store = FakeStore()
    run(store, reader, cfg=dsp_cfg())
    assert reader.post_ids == ["1", "p"] and store.saved[0]["case"]["links"]


def test_row_latest_includes_customer_email_on_the_case():
    store = FakeStore()
    store.prior = {"1": {"case": {"last_modified_at": "2026-11-05T11:00:00Z"}, "verdict": {},
                         "timeline": {"emails": [{"from": "owner@gmail.com", "incoming": True,
                                                  "at": "2026-11-05T11:30:00Z", "text": "any news?"}]}}}
    run(store, FakeReader([case("1")]))
    latest = store.saved[0]["verdict"]["latest"]
    assert latest["party"] == "customer" and latest["text"] == "any news?"


def test_dsp_email_from_merchant_is_labelled_merchant():
    reader = FakeReader([agg("1", record_type="DSP", third_parties=["AcmeEats"])])
    reader.emails = {"1": [{"who": "owner@gmail.com", "ours": False, "at": "2026-11-05T10:00:00Z",
                            "text": "x"}]}
    store = FakeStore()
    store.partner_rows = [ACME]
    run(store, reader, cfg=dsp_cfg())
    assert store.saved[0]["verdict"]["signal"]["code"] == "customer_replied_email"


def ack_reader(text):
    reader = FakeReader([agg("1", record_type="DSP", third_parties=["AcmeEats"])])
    reader.emails = {"1": [{"who": "ops@acmeeats.test", "ours": False, "text": text,
                            "at": "2026-11-05T10:00:00Z"}]}
    return reader


def test_a_plain_ack_keeps_waiting_on_the_aggregator():
    store = FakeStore()
    store.partner_rows = [ACME]
    run(store, ack_reader("Noted, let me check"), cfg=dsp_cfg())
    verdict_ = store.saved[0]["verdict"]
    assert verdict_["ball"] == "third_party" and verdict_["signal"]["code"] == "third_party_ack"


def test_a_real_reply_with_no_llm_still_needs_action():
    store = FakeStore()
    store.partner_rows = [ACME]
    run(store, ack_reader("The issue is caused by a mismatch in the tax config for this outlet."),
        cfg=dsp_cfg())
    assert store.saved[0]["verdict"]["ball"] == "us"


def test_a_stored_label_drives_the_action():
    from interpreter.monitor import replies

    text = "Our engineering team is fixing this defect and will share a date."
    store = FakeStore()
    store.partner_rows = [ACME]
    store.labels = {replies.text_hash(text): {"kind": "bug_raised", "summary": "Fix under way"}}
    run(store, ack_reader(text), cfg=dsp_cfg())
    verdict_ = store.saved[0]["verdict"]
    assert verdict_["signal"]["code"] == "reply_bug_raised"
    assert verdict_["signal"]["text"] == "Fix under way" and "bug" in verdict_["next_action"]


def test_digest_events():
    old = verdict.compute(case("1"), [], merge({}), __import__("datetime").datetime(
        2026, 11, 5, tzinfo=__import__("datetime").timezone.utc))
    old.update(overdue=False, missing_link=False, latest_reply_at="2026-11-01T00:00:00Z")
    prior = {"1": {"verdict": old}}
    new = {**old, "overdue": True, "missing_link": True, "latest_reply_at": "2026-11-02T00:00:00Z"}
    rows = [{"case": case("1"), "verdict": new}, {"case": case("2"), "verdict": old}]
    assert {e["kind"] for e in sync.diff_events(prior, rows)} == {
        "new_reply", "newly_overdue", "newly_missing_link", "new_case"}


def _v(**kw):
    return {"ball": "third_party", "overdue": False, "missing_link": False, **kw}


def _kinds(before, after):
    return {e["kind"] for e in sync.diff_events({"1": {"verdict": before}},
                                                [{"case": case("1"), "verdict": after}])}


def test_a_real_update_raises_an_action_alert():
    after = _v(ball="us", signal={"code": "reply_solution", "text": "Fix shared"},
               latest_reply_at="2026-11-02T00:00:00Z")
    assert "needs_action" in _kinds(_v(latest_reply_at="2026-11-01T00:00:00Z"), after)


def test_a_case_that_stays_on_the_same_action_does_not_alert_again():
    same = _v(ball="us", signal={"code": "reply_solution"})
    assert "needs_action" not in _kinds(same, same)


def test_a_new_reason_to_act_alerts_again():
    before = _v(ball="us", signal={"code": "reply_needs_info"})
    after = _v(ball="us", signal={"code": "customer_replied_email"})
    assert "needs_action" in _kinds(before, after)


def test_an_ack_raises_no_reply_alert():
    after = _v(signal={"code": "third_party_ack"}, latest_reply_at="2026-11-02T00:00:00Z")
    assert _kinds(_v(latest_reply_at="2026-11-01T00:00:00Z"), after) == set()


def test_going_quiet_alerts_once():
    stale = _v(stale=True, next_action="Nudge")
    assert "newly_stale" in _kinds(_v(), stale) and "newly_stale" not in _kinds(stale, stale)


def test_first_sync_has_no_digest_events():
    assert sync.diff_events({}, [{"case": case("1"), "verdict": {}}]) == []


def test_demo_scenarios_classify_correctly(now):
    data = demo.build(now)
    got = {}
    for c in data["cases"]:
        evidence = [data["evidence"][x["url"]] for x in c["links"]]
        got[c["subject"]] = verdict.compute(c, evidence, demo.CONFIG, now)
    assert got["Orders not relaying to POS"]["mismatch"]
    assert "Verify the fix" in got["Menu sync failing after update"]["next_action"]
    assert got["Rider not assigned on delivery orders"]["ball"] == "us"
    assert got["Commission mismatch on settlement"]["missing_link"]
    assert got["Store timings not updating"]["ball"] == "us"
    assert got["Payout failed for outlet A"]["ball"] == "engineering"
    assert got["Stale commission thread"]["stale"]
    assert got["Stale commission thread"]["ball"] == "third_party"
    on_track = got["Coupon stopped applying at checkout"]
    assert on_track["ball"] == "engineering" and on_track["level"] == "low"
    assert len(data["cases"]) >= 8


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@pytest.fixture
def http(monkeypatch):
    calls, queue = [], []

    def get(url, headers=None, params=None, timeout=None):
        calls.append(("GET", url, params))
        return queue.pop(0)

    def fail(*a, **k):
        raise AssertionError("readers must not write")

    monkeypatch.setattr(readers.requests, "get", get)
    monkeypatch.setattr(readers.requests, "post", fail)
    monkeypatch.setattr(readers.time, "sleep", lambda s: None)
    return calls, queue


LINK = {"url": GH, "kind": "github", "owner": "o", "repo": "r", "number": 1}
GH2 = "https://github.com/o/r/issues/2"
LINK2 = {"url": GH2, "kind": "github", "owner": "o", "repo": "r", "number": 2}


class LabelledGithub:
    """Issue 1 is linked to a case; issue 2 is only found by its label."""

    def __init__(self, listed=(LINK2,), states=None, list_error=None, fetch_error=()):
        self.listed, self.states = list(listed), states or {}
        self.list_error, self.fetch_error = list_error, fetch_error
        self.asked = None

    def open_issue_links(self, owner, repo, label):
        self.asked = (owner, repo, label)
        if self.list_error:
            raise self.list_error
        return self.listed

    closed: list = []
    closed_error = None

    def closed_issues(self, owner, repo, label, since):
        if self.closed_error:
            raise self.closed_error
        return self.closed

    def fetch(self, link):
        if link["number"] in self.fetch_error:
            raise requests.ConnectionError("down")
        return {"kind": "github", "url": link["url"], "label": f"o/r#{link['number']}", "ok": True,
                "state": self.states.get(link["number"], "open"), "title": "T", "messages": []}


def test_sync_lists_labelled_issues_that_no_case_links():
    store, github = FakeStore(), LabelledGithub()
    run(store, FakeReader([case("1")]), {"github": github})
    assert github.asked == ("o", "r", "support")
    assert sorted(store.issues_saved) == [GH, GH2]
    assert sorted(e["url"] for e in store.evidence) == [GH, GH2]


def test_sync_saves_closed_issues_with_who_raised_them():
    github = LabelledGithub()
    github.closed = [{"url": GH2, "title": "T", "labels": ["invalid"], "closed_at": "2026-11-01T00:00:00Z",
                      "opened_at": "2026-10-30T00:00:00Z", "body": "**Issue Reporter:** asha | Support\n"}]
    store = FakeStore()
    run(store, FakeReader([case("1")]), {"github": github}, merge({"github_reporter_field": "Issue Reporter"}))
    assert store.closed_saved[0]["reporter"] == "asha" and "body" not in store.closed_saved[0]


def test_closed_issues_failing_only_notes_an_error():
    github = LabelledGithub()
    github.closed_error = requests.ConnectionError("down")
    result = run(FakeStore(), FakeReader([case("1")]), {"github": github})
    assert result["status"] == "partial" and "closed GitHub issues" in result["errors"][0]["error"]


def test_an_issue_that_left_the_list_is_closed():
    store = FakeStore()
    store.issue_rows = {GH + "9": {"is_open": True}, GH + "8": {"is_open": False}}
    run(store, FakeReader([case("1")]), {"github": LabelledGithub()})
    assert store.issues_closed == [GH + "9"]


def test_a_linked_issue_closed_on_github_is_closed_here():
    store = FakeStore()
    store.issue_rows = {GH: {"is_open": True}}
    run(store, FakeReader([case("1")]), {"github": LabelledGithub(listed=[], states={1: "closed"})})
    assert store.issues_closed == [GH] and GH not in store.issues_saved


def test_a_failed_listing_closes_nothing_and_says_so():
    store = FakeStore()
    store.issue_rows = {GH + "9": {"is_open": True}}
    result = run(store, FakeReader([case("1")]),
                 {"github": LabelledGithub(list_error=requests.ConnectionError("down"))})
    assert store.issues_closed == [] and result["status"] == "partial"
    assert "not listed" in result["errors"][0]["error"]


def test_issues_that_failed_to_load_each_keep_a_row():
    store = FakeStore()
    run(store, FakeReader([case("1")]), {"github": LabelledGithub(fetch_error=(1, 2))})
    assert sorted(store.issues_saved) == [GH, GH2]
    assert store.issues_closed == []


def test_one_issue_linked_by_two_urls_is_read_once():
    link = {**LINK, "url": GH + "#issuecomment-5"}
    store = FakeStore()
    c = case("1", url=link["url"])
    run(store, FakeReader([c]), {"github": LabelledGithub(listed=[LINK])})
    assert list(store.issues_saved) == [GH]


def test_a_broken_issue_step_does_not_stop_the_queue():
    class Broken(FakeStore):
        def issue_reads(self, tenant_id):
            raise RuntimeError("no table")

    store = Broken()
    result = run(store, FakeReader([case("1")]), {"github": LabelledGithub()})
    assert result["status"] == "partial" and len(store.saved) == 1


def test_github_lists_issues_by_label_across_pages_without_pull_requests(http):
    calls, queue = http
    row = {"html_url": GH, "number": 1}
    queue += [Resp(200, [row] * 99 + [{**row, "pull_request": {}}]), Resp(200, [{"html_url": GH2, "number": 2}])]
    links = readers.GithubReader("t", []).open_issue_links("o", "r", "support")
    assert len(links) == 100 and links[-1] == LINK2
    assert calls[0][2]["labels"] == "support" and calls[0][2]["state"] == "open" and calls[1][2]["page"] == 2


def test_github_listing_raises_rather_than_return_part_of_the_list(http):
    queue = http[1]
    queue += [Resp(200, [{"html_url": GH, "number": 1}] * 100), Resp(403, {})]
    with pytest.raises(requests.HTTPError):
        readers.GithubReader("t", []).open_issue_links("o", "r", "support")
    with pytest.raises(ValueError):
        readers.GithubReader(None, []).open_issue_links("o", "r", "support")


def test_github_keeps_the_description_and_long_comments(http):
    queue = http[1]
    long = "x" * 1200
    queue += [Resp(200, {"user": {"login": "lead"}, "state": "open", "title": "T", "labels": [], "body": "Printer down"}),
              Resp(200, [{"user": {"login": "dev"}, "created_at": "2026-01-01T00:00:00Z",
                          "html_url": "c", "body": long}])]
    ev = readers.GithubReader("t", []).fetch(LINK)
    assert ev["body"] == "Printer down" and ev["messages"][0]["text"] == long


def test_github_normalises_and_paginates(http):
    calls, queue = http
    page = [{"user": {"login": "dev"}, "created_at": "2026-01-01T00:00:00Z",
             "html_url": "c", "body": "x?Signature=s"}] * 100
    queue += [Resp(200, {"user": {"login": "lead"}, "state": "open", "title": "T", "labels": []}),
              Resp(200, page),
              Resp(200, [{"user": {"login": "Lead"}, "created_at": "2026-01-02T00:00:00Z",
                          "html_url": "d", "body": "y"},
                         {"user": {"login": "ci", "type": "Bot"}, "body": "z"}])]
    ev = readers.GithubReader("t", []).fetch(LINK)
    assert ev["ok"] and len(ev["messages"]) == 101
    assert ev["messages"][-1]["ours"] and not ev["messages"][0]["ours"]
    assert all(c[0] == "GET" for c in calls)


def test_configured_github_bot_accounts_are_ignored(http):
    _, queue = http
    queue += [Resp(200, {"user": {"login": "lead"}, "state": "open"}),
              Resp(200, [{"user": {"login": "HelperBot"}, "created_at": "2026-01-02T00:00:00Z",
                          "body": "summary"},
                         {"user": {"login": "dev"}, "created_at": "2026-01-01T00:00:00Z",
                          "body": "real"}])]
    ev = readers.GithubReader("t", [], ["helperbot"]).fetch(LINK)
    assert [m["who"] for m in ev["messages"]] == ["dev"]


def test_github_support_users_are_ours(http):
    _, queue = http
    queue += [Resp(200, {"user": {"login": "x"}, "state": "open"}),
              Resp(200, [{"user": {"login": "agent"}, "created_at": "t", "body": "b"}])]
    assert readers.GithubReader("t", ["Agent"]).fetch(LINK)["messages"][0]["ours"]


@pytest.mark.parametrize("status", [403, 404])
def test_github_denied_is_a_readable_error(http, status):
    http[1].append(Resp(status, {}))
    ev = readers.GithubReader("t", []).fetch(LINK)
    assert not ev["ok"] and ev["error"] == "not accessible: check token scope for o/r"


def test_retries_on_429(http):
    _, queue = http
    queue += [Resp(429, {}, {"Retry-After": "1"}), Resp(404, {})]
    assert "not accessible" in readers.GithubReader("t", []).fetch(LINK)["error"]


SLACK = {"url": "https://x.slack.com/archives/C1/p1700000000000100", "kind": "slack",
         "channel": "C1", "ts": "1700000000.000100", "thread_ts": "1700000000.000100"}


def _user(email):
    return Resp(200, {"ok": True, "user": {"real_name": "N", "profile": {"email": email}}})


def test_slack_labels_ours_and_drops_noise(http):
    _, queue = http
    queue += [Resp(200, {"ok": True, "messages": [
        {"user": "U1", "ts": "1700000000.000100", "text": "hello"},
        {"user": "U2", "ts": "1700000001.000100", "text": "from partner"},
        {"user": "U1", "ts": "1700000002.000100", "text": "<@U9> cc"},
        {"user": "U1", "ts": "1700000003.000100", "text": "<@U9>  "},
        {"bot_id": "B1", "ts": "1700000004.000100", "text": "bot"}]}),
        _user("a@acme.com"), _user("b@partnera.in")]
    ev = readers.SlackReader(["t"], "acme.com").fetch(SLACK)
    assert [m["ours"] for m in ev["messages"]] == [True, False]


def test_slack_lookalike_domain_is_not_ours(http):
    _, queue = http
    queue += [Resp(200, {"ok": True, "messages": [{"user": "U1", "ts": "1.000100", "text": "x"}]}),
              _user("a@notacme.com")]
    ev = readers.SlackReader(["t"], "acme.com").fetch(SLACK)
    assert not ev["messages"][0]["ours"]


@pytest.mark.parametrize("code,words", [("channel_not_found", "join the channel"),
                                        ("not_in_channel", "join it"),
                                        ("missing_scope", "reconnect"),
                                        ("token_revoked", "reconnect Slack")])
def test_slack_errors_are_readable(http, code, words):
    http[1].append(Resp(200, {"ok": False, "error": code}))
    ev = readers.SlackReader(["t"], "x.com").fetch(SLACK)
    assert not ev["ok"] and words in ev["error"]


def test_slack_tries_the_next_lead_token(http):
    _, queue = http
    queue += [Resp(200, {"ok": False, "error": "not_in_channel"}),
              Resp(200, {"ok": True, "messages": []})]
    assert readers.SlackReader(["a", "b"], "x.com").fetch(SLACK)["ok"]


def test_slack_without_a_login_says_so(http):
    assert "connect Slack" in readers.SlackReader([], "x.com").fetch(SLACK)["error"]


def test_slack_via_claude_says_how_to_refresh(http):
    ev = readers.SlackReader([], "x.com", via_claude=True).fetch(SLACK)
    assert "refresh Slack" in ev["error"]


def test_jira_is_linked_but_not_checked():
    c = readers.EvidenceCollector({})
    url = "https://x.atlassian.net/browse/A-1"
    ev = c.collect([{"url": url, "kind": "jira"}])[url]
    assert ev["note"] == "linked, status not checked" and ev["error"] is None


class FakeSF:
    sf_instance = "x.my.salesforce.com"

    def __init__(self, rows=None):
        self.soql, self.rows = [], rows or []

    def query_all(self, soql):
        self.soql.append(soql)
        return {"records": self.rows}

    def __getattr__(self, name):
        raise AssertionError(f"reader used sf.{name}; only query_all is allowed")


CFG = merge({"owner_username": "lead@x.io", "waiting_values": {"Engineer": "engineering"},
             "customer_reply_values": ["Customer Replied"],
             "fields": {"waiting_on": "Waiting_Party__c", "link": "Link__c",
                        "third_party": "Partner__c", "last_reply_from": "Activity__c"}})


def test_salesforce_query_shape_and_mapping():
    sf = FakeSF([{"Id": "1", "Subject": "s", "Waiting_Party__c": "Engineer", "Link__c": GH,
                  "Partner__c": "PartnerA;PartnerB", "Activity__c": "Customer Replied",
                  "RecordType": {"Name": "Support"}, "Owner": {"Name": "Q"}}])
    got = readers.SalesforceCaseReader(sf, CFG).list_dependency_cases()[0]
    assert "IsClosed = false AND Waiting_Party__c IN ('Engineer')" in sf.soql[0]
    assert "Username = 'lead@x.io'" in sf.soql[0]
    assert got["third_parties"] == ["PartnerA", "PartnerB"]
    assert got["waiting_party"] == "engineering" and got["last_reply_from"] == "customer"
    assert got["links"][0]["kind"] == "github"
    assert got["url"].endswith("/lightning/r/Case/1/view")


def test_salesforce_needs_waiting_config():
    with pytest.raises(ValueError):
        readers.SalesforceCaseReader(FakeSF(), merge({})).list_dependency_cases()


def test_every_salesforce_read_only_uses_query_all():
    sf = FakeSF()
    r = readers.SalesforceCaseReader(sf, CFG)
    r.timeline("1"), r.children_of(["1"]), r.resolved_since("2026-01-01T00:00:00Z")
    r.get_cases(["1"]), r.cases_by_number(["00000001"])
    assert all(q.startswith("SELECT") for q in sf.soql)


def test_org_without_record_types_still_works():
    class NoRecordTypes(FakeSF):
        def query_all(self, soql):
            self.soql.append(soql)
            if "RecordType" in soql:
                raise RuntimeError("INVALID_FIELD: No such column RecordType")
            return {"records": [{"Id": "1", "Status": "New"}]}

    sf = NoRecordTypes()
    r = readers.SalesforceCaseReader(sf, CFG)
    assert r.list_dependency_cases()[0]["record_type"] is None
    assert "RecordType" not in sf.soql[-1]


def test_an_unrelated_salesforce_error_still_raises():
    class Broken(FakeSF):
        def query_all(self, soql):
            raise RuntimeError("REQUEST_LIMIT_EXCEEDED")

    with pytest.raises(RuntimeError):
        readers.SalesforceCaseReader(Broken(), CFG).list_dependency_cases()


def test_unsent_email_drafts_are_never_read():
    sf = FakeSF()
    r = readers.SalesforceCaseReader(sf, CFG)
    r.timeline("1"), r.latest_emails(["1"]), list(r.resolved_conversations("DSP", "a", "b"))
    assert not [q for q in sf.soql if "FROM EmailMessage" in q and "Status != '5'" not in q]


def test_soql_values_are_escaped():
    sf = FakeSF()
    readers.SalesforceCaseReader(sf, CFG).timeline("a' OR Id != '")
    assert "a\\' OR" in sf.soql[0]


def test_no_secret_or_signed_url_reaches_saved_rows():
    sf = FakeSF([{"Id": "1", "Subject": "x https://s3.io/a?Signature=sek&AWSAccessKeyId=KEY",
                  "Description": "y" * 900, "Waiting_Party__c": "Engineer"}])
    got = readers.SalesforceCaseReader(sf, CFG).list_dependency_cases()[0]
    assert "sek" not in got["subject"] and "KEY" not in got["subject"]
    assert len(got["description"]) <= 401




def test_older_emails_keep_a_textless_log_but_no_text():
    from interpreter.monitor import sync as s
    cfg = {"dependency_record_type": "DSP", "internal_email_domains": ["us.com"], "third_party_label": "T"}
    mails = [{"who": "a@us.com", "ours": True, "at": f"2026-11-0{9 - i}T00:00:00Z", "text": f"mail {i}"}
             for i in range(5)]
    mails.append({"who": "z@acme.com", "ours": False, "at": "2026-10-01T00:00:00Z",
                  "text": "Here is the root cause of the problem"})

    class Reader:
        def latest_emails(self, ids):
            return {"1": mails}

    case = {"id": "1", "record_type": "DSP", "waiting_party": "third_party", "url": "u",
            "partner": {"email_domains": ["acme.com"]}}
    s._dsp_emails([case], Reader(), cfg, [])
    assert len(case["email_evidence"]["messages"]) == 5
    old = case["email_log"][-1]
    assert old["party"] == "third_party" and old["ack"] is False and "text" not in old


def test_becoming_ready_to_close_alerts_once_and_not_as_needs_action():
    ready = _v(ball="us", ready_to_close=True, stale=True,
               signal={"code": "dsp_resolved", "text": "Linked DSP case 900 is Resolved"})
    kinds = _kinds(_v(), ready)
    assert "ready_to_close" in kinds
    assert "needs_action" not in kinds and "newly_stale" not in kinds
    assert "ready_to_close" not in _kinds(ready, ready)


def test_counts_include_ready_to_close():
    rows = [{"verdict": _v(ball="us", mismatch=False, ready_to_close=True, level="high")},
            {"verdict": _v(mismatch=False, level="low")}]
    assert sync.counts(rows)["ready_to_close"] == 1


def test_a_merchant_saying_its_fixed_makes_the_case_ready_to_close():
    class MerchantWrote(FakeReader):
        def timeline(self, case_id):
            return {"emails": [{"from": "owner@shop.test", "incoming": True, "at": "2026-11-05T11:30:00Z",
                                "text": "Its working now"}], "comments": [], "history": []}

    store = FakeStore()
    run(store, MerchantWrote([case("1", last_reply_from="customer")]))
    got = store.saved[0]
    assert got["case"]["customer_reply"]["kind"] == "confirmed"
    assert got["verdict"]["ready_to_close"] and got["verdict"]["signal"]["code"] == "customer_confirmed"
    assert store.finished[1]["ready_to_close"] == 1


def _row(cid, record_type, ball, code=None, latest_reply_at=None, activity=None, **case_kw):
    return {"case": {"id": cid, "record_type": record_type, **case_kw},
            "verdict": {"ball": ball, "signal": {"code": code} if code else None,
                        "latest_reply_at": latest_reply_at, "latest_activity_at": activity}}


CFG_DSP = {"dependency_record_type": "DSP"}
SUPPORT_REF = {"id": "s1", "case_number": "00000043", "record_type": "Support", "url": "https://sf/s1"}


def test_linked_wait_when_support_case_waits_on_merchant_after_the_ask():
    dsp = _row("d1", "DSP", "us", "reply_needs_info", "2026-10-26T11:32:52Z", children=[SUPPORT_REF])
    support = _row("s1", "Support", "customer", "awaiting_customer_report", activity="2026-11-06T05:31:05Z")
    got = sync.linked_wait(dsp, [dsp, support], CFG_DSP)
    assert got["case_number"] == "00000043" and got["url"] == "https://sf/s1"


@pytest.mark.parametrize("support_ball,activity,code", [
    ("us", "2026-11-06T05:31:05Z", "reply_needs_info"),        # support case is our turn
    ("customer", "2026-10-20T00:00:00Z", "reply_needs_info"),  # not worked since the ask
    ("customer", "2026-11-06T05:31:05Z", "waiting_third_party"),  # DSP isn't on us for a reply
])
def test_no_linked_wait(support_ball, activity, code):
    ball = "us" if code != "waiting_third_party" else "third_party"
    dsp = _row("d1", "DSP", ball, code, "2026-10-26T11:32:52Z", parent=SUPPORT_REF)
    support = _row("s1", "Support", support_ball, activity=activity)
    assert sync.linked_wait(dsp, [dsp, support], CFG_DSP) is None


def test_no_linked_wait_for_a_support_case_or_an_unsynced_link():
    support = _row("s1", "Support", "us", "reply_needs_info", children=[{"id": "d9", "record_type": "DSP"}])
    assert sync.linked_wait(support, [support], CFG_DSP) is None
    dsp = _row("d1", "DSP", "us", "reply_needs_info", children=[SUPPORT_REF])
    assert sync.linked_wait(dsp, [dsp], CFG_DSP) is None


class _ByNumber:
    def __init__(self, found, fail=False):
        self.found, self.fail, self.asked = found, fail, None

    def cases_by_number(self, numbers):
        self.asked = numbers
        if self.fail:
            raise RuntimeError("down")
        return {n: self.found[n] for n in numbers if n in self.found}


def _ref(cid, number, record_type, closed=False, links=None):
    return {"id": cid, "case_number": number, "status": "Closed" if closed else "In Progress",
            "closed": closed, "record_type": record_type, "url": f"https://sf/{cid}", "links": links or []}


def _c(cid, number, record_type, subject="", **kw):
    return {"id": cid, "case_number": number, "record_type": record_type, "subject": subject,
            "status": "In Progress", "url": f"https://sf/{cid}", "source": "salesforce",
            "parent": None, "children": [], "links": [], **kw}


def test_named_numbers_skip_the_case_itself_and_repeats():
    c = {"case_number": "00132973", "subject": "Case #00132969–Kulcha King 00132969 ref 00132973 x123456789"}
    assert sync.named_numbers(c) == [("00132969", "subject")]


def test_named_numbers_read_the_subject_first_then_the_description():
    c = {"case_number": "00132756", "subject": "Zomato Menu Issue | Case Number 00132721",
         "description": "Res ID 22901351\nCase Number 00132721\nTicket No: 00132700"}
    assert sync.named_numbers(c) == [("00132721", "subject"), ("22901351", "description"),
                                     ("00132700", "description")]


def test_dsp_named_support_case_is_linked_both_ways():
    from interpreter.monitor.links import parse
    dsp = _c("d1", "00132973", "DSP", "Case #00132969–Kulcha King – Careem store offline")
    support = _c("s1", "00132969", "Support")
    gh = parse("https://github.com/o/r/issues/7")
    reader = _ByNumber({"00132969": _ref("s1", "00132969", "Support", links=gh)})
    errors = []
    sync._named_links([dsp, support], {}, reader, {"dependency_record_type": "DSP"}, errors)
    assert errors == [] and reader.asked == ["00132969"]
    assert dsp["children"] == [{"id": "s1", "case_number": "00132969", "status": "In Progress",
                                "closed": False, "record_type": "Support", "url": "https://sf/s1",
                                "via": "subject"}]
    assert dsp["links"] == gh
    assert support["parent"]["id"] == "d1" and support["parent"]["via"] == "subject"
    assert support["parent"]["closed"] is False and support["parent"]["record_type"] == "DSP"
    # the DSP case is now found from the Support side, as with a Salesforce link
    assert sync._dsp_ids(support, "DSP") == ["d1"]


def test_salesforce_links_win_and_dsp_siblings_are_not_linked():
    linked = _c("d1", "00000001", "DSP", "about 00000002", children=[{"id": "x", "record_type": "Support"}])
    sibling = _c("d2", "00000003", "DSP", "dup of 00000004")
    reader = _ByNumber({"00000004": _ref("d4", "00000004", "DSP")})
    sync._named_links([linked, sibling], {}, reader, {"dependency_record_type": "DSP"}, [])
    assert reader.asked == ["00000004"]
    assert linked["children"] == [{"id": "x", "record_type": "Support"}] and sibling["children"] == []


def test_support_case_keeps_its_subject_parent_after_the_dsp_closes():
    support = _c("s1", "00132969", "Support")
    prior = {"s1": {"case": {"parent": {"id": "d1", "case_number": "00132973", "record_type": "DSP",
                                       "via": "subject"}}}}
    reader = _ByNumber({"00132973": _ref("d1", "00132973", "DSP", closed=True)})
    sync._named_links([support], prior, reader, {"dependency_record_type": "DSP"}, [])
    assert support["parent"] == {"id": "d1", "case_number": "00132973", "status": "Closed", "closed": True,
                                 "record_type": "DSP", "url": "https://sf/d1", "via": "subject"}


def test_subject_lookup_failure_is_recorded_and_changes_nothing():
    dsp = _c("d1", "00132973", "DSP", "Case #00132969")
    errors = []
    sync._named_links([dsp], {}, _ByNumber({}, fail=True), {"dependency_record_type": "DSP"}, errors)
    assert dsp["children"] == [] and "descriptions not read" in errors[0]["error"]


def test_reader_without_number_lookup_is_skipped():
    dsp = _c("d1", "00132973", "DSP", "Case #00132969")
    sync._named_links([dsp], {}, object(), {"dependency_record_type": "DSP"}, [])
    assert dsp["children"] == []


def test_cases_by_number_maps_refs_by_number():
    sf = FakeSF([{"Id": "s1", "CaseNumber": "00132969", "Status": "In Progress", "IsClosed": False,
                  "Link__c": GH, "RecordType": {"Name": "Support"}}])
    got = readers.SalesforceCaseReader(sf, CFG).cases_by_number(["00132969", "o'1"])
    assert "WHERE CaseNumber IN ('00132969', 'o\\'1')" in sf.soql[0]
    ref = got["00132969"]
    assert ref["id"] == "s1" and ref["record_type"] == "Support" and ref["closed"] is False
    assert ref["links"][0]["kind"] == "github" and ref["url"].endswith("/lightning/r/Case/s1/view")


def test_support_case_with_a_salesforce_parent_gets_the_subject_dsp_as_a_child():
    talabat = {"id": "d0", "case_number": "00132972", "record_type": "DSP", "closed": False}
    dsp = _c("d1", "00132973", "DSP", "Case #00132969–Kulcha King")
    support = _c("s1", "00132969", "Support", parent=talabat)
    reader = _ByNumber({"00132969": _ref("s1", "00132969", "Support")})
    sync._named_links([dsp, support], {}, reader, {"dependency_record_type": "DSP"}, [])
    assert support["parent"] is talabat
    assert [(x["id"], x["via"]) for x in support["children"]] == [("d1", "subject")]
    assert sync._dsp_ids(support, "DSP") == ["d0", "d1"]


def test_carried_subject_links_are_not_doubled_while_the_dsp_is_still_queued():
    dsp = _c("d1", "00132973", "DSP", "Case #00132969")
    support = _c("s1", "00132969", "Support", parent={"id": "d0", "record_type": "DSP"})
    prior = {"s1": {"case": {"children": [{"id": "d1", "case_number": "00132973", "record_type": "DSP",
                                           "via": "subject"}]}}}
    reader = _ByNumber({"00132969": _ref("s1", "00132969", "Support"),
                        "00132973": _ref("d1", "00132973", "DSP")})
    sync._named_links([dsp, support], prior, reader, {"dependency_record_type": "DSP"}, [])
    assert sorted(reader.asked) == ["00132969", "00132973"]
    assert [x["id"] for x in support["children"]] == ["d1"]


def test_a_closed_subject_dsp_child_is_carried_with_its_closed_state():
    support = _c("s1", "00132969", "Support", parent={"id": "d0", "record_type": "DSP", "closed": False})
    prior = {"s1": {"case": {"children": [{"id": "d1", "case_number": "00132973", "record_type": "DSP",
                                           "via": "subject"}]}}}
    reader = _ByNumber({"00132973": _ref("d1", "00132973", "DSP", closed=True)})
    sync._named_links([support], prior, reader, {"dependency_record_type": "DSP"}, [])
    assert support["children"][0]["closed"] is True and support["children"][0]["via"] == "subject"


def test_children_of_reads_whether_each_child_is_closed():
    sf = FakeSF([{"Id": "c1", "CaseNumber": "1", "Status": "Closed", "IsClosed": True, "ParentId": "p1",
                  "RecordType": {"Name": "DSP"}}])
    got = readers.SalesforceCaseReader(sf, CFG).children_of(["p1"])
    assert "IsClosed" in sf.soql[0] and got["p1"][0]["closed"] is True


def test_description_names_the_support_case_and_order_ids_fall_away():
    dsp = _c("d1", "00132756", "DSP", "Zomato Menu Issue | Samosa Party",
             description="Res ID 22901351\nOrder 12345678\nCase Number 00132721\nTicket No: 00132700")
    support = _c("s1", "00132721", "Support")
    reader = _ByNumber({"00132721": _ref("s1", "00132721", "Support"),
                        "00132700": _ref("d0", "00132700", "DSP")})
    sync._named_links([dsp, support], {}, reader, {"dependency_record_type": "DSP"}, [])
    assert sorted(reader.asked) == ["00132700", "00132721", "12345678", "22901351"]
    assert [(x["id"], x["via"]) for x in dsp["children"]] == [("s1", "description")]
    assert support["parent"]["id"] == "d1" and support["parent"]["via"] == "description"


def test_at_most_three_named_cases_are_linked():
    numbers = [f"0013000{i}" for i in range(5)]
    dsp = _c("d1", "00139999", "DSP", "x", description=" ".join(numbers))
    reader = _ByNumber({n: _ref(f"s{n}", n, "Support") for n in numbers})
    sync._named_links([dsp], {}, reader, {"dependency_record_type": "DSP"}, [])
    assert [x["case_number"] for x in dsp["children"]] == numbers[:sync.NAMED_CAP]


def test_description_links_are_carried_with_their_via():
    support = _c("s1", "00132721", "Support")
    prior = {"s1": {"case": {"parent": {"id": "d1", "case_number": "00132756", "record_type": "DSP",
                                        "via": "description"}}}}
    reader = _ByNumber({"00132756": _ref("d1", "00132756", "DSP", closed=True)})
    sync._named_links([support], prior, reader, {"dependency_record_type": "DSP"}, [])
    assert support["parent"]["closed"] is True and support["parent"]["via"] == "description"
