from datetime import datetime, timedelta, timezone

from interpreter.monitor import issue_status as st
from interpreter.monitor import tech_pending as tp

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
CFG = {"dependency_record_type": "DSP", "github_squad_suffix": "-squad"}
URL = "https://github.com/o/r/issues/8392"
LINK = {"kind": "github", "url": URL, "owner": "o", "repo": "r", "number": 8392}


def ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def issue(days=10, messages=(), **kw):
    return {"url": URL, "label": "o/r#8392", "ok": True, "state": "open", "title": "POS IDs missing",
            "labels": ["support", "order-squad"], "opened_at": ago(days), "messages": list(messages), **kw}


def case(**kw):
    return {"id": "1", "case_number": "42", "record_type": "Support", "priority": "Urgent",
            "target_at": ago(3), "links": [LINK], **kw}


def read(status=st.CAUSE_GIVEN, **kw):
    return {"url": URL, "status": status, "waiting_on": "third_party", "reason": "Waiting on the partner", **kw}


def build(ev=None, cases=None, reads=None, days=7):
    return tp.build([read()] if reads is None else reads, [case()] if cases is None else cases,
                    {URL: ev} if ev else {}, days, NOW, CFG)


def only(got):
    return got["squads"][0]["issues"][0]


def test_an_issue_shows_the_given_format_and_the_reading():
    msg = {"who": "agent", "at": ago(2), "ours": True, "text": "Any update?"}
    r = only(build(issue(messages=[msg])))
    assert (r["squad"], r["issue"], r["last_reply_by"], r["days_open"]) == ("order-squad", "8392", "agent", 10)
    assert (r["priority"], r["sla_status"]) == ("Urgent", "Breached by 3d 0h")
    assert (r["status"], r["tech_status"]) == (st.CAUSE_GIVEN, "Cause given, not closed yet")
    assert (r["waiting_on"], r["reason"]) == ("third_party", "Waiting on the partner")


def test_every_status_stays_on_the_page():
    for status in (st.NO_REPLY, st.INVESTIGATING, st.CAUSE_GIVEN, st.ANSWERED, st.NOT_READ):
        assert only(build(issue(), reads=[read(status)]))["status"] == status


def test_an_issue_with_no_open_case_is_listed_with_no_case():
    r = only(build(issue(), cases=[]))
    assert (r["case_id"], r["case_number"], r["priority"], r["sla_status"]) == (None, None, None, None)


def test_a_link_in_another_url_form_still_finds_the_case():
    other = case(links=[{**LINK, "url": "https://www.github.com/o/r/issues/8392#issuecomment-1"}])
    assert only(build(issue(), cases=[other]))["case_number"] == "42"


def test_new_issues_are_listed_and_marked_by_the_age_limit():
    assert only(build(issue(days=7)))["over_days"] is False
    assert only(build(issue(days=7.1)))["over_days"] is True


def test_a_closed_issue_is_left_out():
    assert build(issue(state="closed"))["total"] == 0


def test_an_issue_that_failed_to_load_is_counted_not_listed():
    got = build(issue(ok=False, state=None))
    assert (got["total"], got["unread"]) == (0, 1)
    assert build(None)["unread"] == 1


def test_a_reading_with_no_status_shows_as_not_read():
    assert only(build(issue(), reads=[{"url": URL}]))["status"] == st.NOT_READ


def test_the_main_case_is_the_support_case_then_the_most_urgent():
    dsp = case(id="2", case_number="43", record_type="DSP", priority="Urgent")
    low = case(id="3", case_number="44", priority="Low")
    r = only(build(issue(), cases=[dsp, low, case()]))
    assert r["case_number"] == "42" and sorted(r["other_cases"]) == ["43", "44"]


def test_issues_group_by_squad_with_the_biggest_first():
    second = {**issue(), "url": URL + "0", "label": "o/r#83920", "labels": ["data-squad"]}
    third = {**issue(), "url": URL + "1", "label": "o/r#83921", "labels": ["data-squad"]}
    evidence = {e["url"]: e for e in (issue(), second, third)}
    reads = [read(url=u) for u in evidence]
    got = tp.build(reads, [], evidence, 7, NOW, CFG)
    assert [s["squad"] for s in got["squads"]] == ["data-squad", "order-squad"]


def test_an_issue_with_no_squad_label_says_so():
    assert only(build(issue(labels=["support"])))["squad"] == tp.NO_SQUAD


def test_every_business_with_an_open_case_is_listed_once():
    same = {"business_name": "Biz One", "biz_id": "111"}
    cases = [case(**same), case(id="2", case_number="43", **same),
             case(id="3", case_number="44", business_name="Biz Two", business_id="222")]
    row = only(build(issue(), cases))
    assert row["businesses"] == [{"business": "Biz One", "biz_id": "111"}, {"business": "Biz Two", "biz_id": "222"}]


def test_an_issue_with_no_business_lists_none():
    assert only(build(issue()))["businesses"] == []


def test_the_last_engineer_and_support_replies_are_dated_and_every_case_listed():
    tech = {"who": "eng", "at": ago(4), "ours": False, "text": "Fixed, released"}
    thanks = {"who": "agent", "at": ago(3), "ours": True, "text": "Thanks, checking with Mx"}
    r = only(build(issue(messages=[tech, thanks]), cases=[case(), case(id="2", case_number="43")]))
    assert (r["last_tech_reply_at"], r["last_support_reply_at"]) == (ago(4), ago(3))
    assert sorted(r["case_ids"]) == ["1", "2"]


def test_an_issue_no_case_links_says_who_raised_it_and_for_which_business():
    body = "**Issue Reporter:** rishitha.y | Support \n\n**Biz ID:** 53794636  \n**Biz Name:** Five Thirty\n"
    r = only(build(issue(body=body), cases=[]))
    assert (r["reporter"], r["biz_id"], r["biz_name"], r["case_ids"]) == ("rishitha.y", "53794636", "Five Thirty", [])
