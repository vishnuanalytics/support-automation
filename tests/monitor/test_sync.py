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


class OkGithub:
    calls = 0

    def fetch(self, link):
        OkGithub.calls += 1
        return {"kind": "github", "url": link["url"], "ok": True, "state": "open", "messages": []}


class ExplodingGithub:
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
    assert reader.email_ids == ["d"]
    assert store.saved[0]["verdict"]["ball"] == "third_party"
    assert store.saved[0]["case"]["email_evidence"]["related"]


def test_support_case_emails_are_not_third_party_evidence():
    reader = FakeReader([agg("1", record_type="Support")])
    reader.emails = {"1": [EMAIL]}
    store = FakeStore()
    result = run(store, reader, cfg=dsp_cfg())
    assert result["missing_link"] == 1 and result["my_turn"] == 0


def test_child_links_count_for_the_parent():
    class WithChild(FakeReader):
        def children_of(self, ids):
            from interpreter.monitor.links import parse
            return {"1": [{"id": "d", "record_type": "DSP", "links": parse(GH)}]}

    store = FakeStore()
    result = run(store, WithChild([agg("1")]), cfg=dsp_cfg())
    assert result["missing_link"] == 0
    assert "links" not in store.saved[0]["case"]["children"][0]


def test_digest_events():
    old = verdict.compute(case("1"), [], merge({}), __import__("datetime").datetime(
        2026, 11, 5, tzinfo=__import__("datetime").timezone.utc))
    old.update(overdue=False, missing_link=False, latest_reply_at="2026-11-01T00:00:00Z")
    prior = {"1": {"verdict": old}}
    new = {**old, "overdue": True, "missing_link": True, "latest_reply_at": "2026-11-02T00:00:00Z"}
    rows = [{"case": case("1"), "verdict": new}, {"case": case("2"), "verdict": old}]
    assert {e["kind"] for e in sync.diff_events(prior, rows)} == {
        "new_reply", "newly_overdue", "newly_missing_link", "new_case"}


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
    r.get_cases(["1"])
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


