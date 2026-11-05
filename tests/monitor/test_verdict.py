from datetime import datetime, timedelta, timezone

import pytest

from interpreter.monitor import verdict as v

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


def case(**kw):
    base = {"id": "1", "waiting_party": "engineering", "waiting_raw": "Engineer",
            "last_modified_at": ago(1), "priority": "Medium", "url": "https://sf/1",
            "links": [{"kind": "github"}], "third_parties": []}
    return {**base, **kw}


def gh(*msgs, state="open"):
    return {"kind": "github", "ok": True, "state": state, "url": "https://gh/1",
            "messages": list(msgs)}


def slack(*msgs):
    return {"kind": "slack", "ok": True, "url": "https://slack/1", "messages": list(msgs)}


def msg(ours, hours=2, who="dev"):
    return {"who": who, "ours": ours, "at": ago(hours), "text": "x", "url": "https://src/1"}


def codes(r):
    return {x["code"] for x in r["reasons"]}


@pytest.mark.parametrize("raw", ["2026-10-30T06:07:33.000+0000", "2026-10-30T06:07:33.000Z",
                                 "2026-10-30T06:07:33+00:00"])
def test_parse_salesforce_datetimes(raw):
    assert v.parse_dt(raw) == datetime(2026, 10, 30, 6, 7, 33, tzinfo=timezone.utc)


@pytest.mark.parametrize("raw", [None, "", "not a date"])
def test_parse_bad_datetimes(raw):
    assert v.parse_dt(raw) is None


def test_github_closed_is_our_turn(cfg):
    r = v.compute(case(), [gh(state="closed")], cfg, NOW)
    assert r["ball"] == "us" and r["mismatch"]
    assert "Verify the fix" in r["next_action"]


def test_engineer_replied_is_our_turn_and_cites_comment(cfg):
    r = v.compute(case(), [gh(msg(False))], cfg, NOW)
    assert r["ball"] == "us" and r["mismatch"]
    reason = next(x for x in r["reasons"] if x["code"] == "mismatch")
    assert reason["source"]["url"] == "https://src/1"


def test_our_comment_last_is_engineers_turn(cfg):
    r = v.compute(case(), [gh(msg(False, 5), msg(True, 2))], cfg, NOW)
    assert r["ball"] == "engineering" and not r["mismatch"]


def test_no_comments_is_engineers_turn(cfg):
    assert v.compute(case(), [gh()], cfg, NOW)["ball"] == "engineering"


def agg(**kw):
    return case(waiting_party="third_party", waiting_raw="Aggregator", **kw)


def test_external_slack_last_is_our_turn(cfg):
    r = v.compute(agg(), [slack(msg(True, 5), msg(False, 2, "PartnerB ops"))], cfg, NOW)
    assert r["ball"] == "us" and r["mismatch"]


def test_our_slack_last_is_aggregators_turn(cfg):
    r = v.compute(agg(), [slack(msg(False, 5), msg(True, 2))], cfg, NOW)
    assert r["ball"] == "third_party"


def test_internal_slack_on_engineer_case_cant_set_turn_but_counts_for_staleness(cfg):
    c = case(last_modified_at=ago(100))
    r = v.compute(c, [slack(msg(False, 2))], cfg, NOW)
    assert r["ball"] == "unknown" and r["stale_hours"] == 2 and not r["stale"]


def test_customer_replied_is_our_turn(cfg):
    r = v.compute(case(last_reply_from="customer"), [gh(msg(True))], cfg, NOW)
    assert r["ball"] == "us" and r["mismatch"] and "customer_replied" in codes(r)


@pytest.mark.parametrize("c,words", [(case(links=[]), "on-call"), (agg(links=[]), "DSP")])
def test_missing_link(cfg, c, words):
    r = v.compute(c, [], cfg, NOW)
    assert r["ball"] == "unknown" and r["missing_link"] and words in r["next_action"]


def test_missing_link_keeps_our_turn_when_customer_replied(cfg):
    r = v.compute(agg(links=[], last_reply_from="customer"), [], cfg, NOW)
    assert r["ball"] == "us" and r["missing_link"]


def test_us_wins_over_conflicting_signal(cfg):
    r = v.compute(case(), [gh(msg(True)), gh(state="closed")], cfg, NOW)
    assert r["ball"] == "us"


def test_waiting_party_preferred_over_other_party(cfg):
    c = agg()
    r = v.compute(c, [gh(msg(True)), slack(msg(True))], cfg, NOW)
    assert r["ball"] == "third_party"


def test_unreadable_link(cfg):
    ev = {"kind": "github", "ok": False, "url": "https://gh/1", "error": "not accessible"}
    r = v.compute(case(), [ev], cfg, NOW)
    assert r["ball"] == "unknown" and r["score"] == 5 and "Fix link access" in r["next_action"]


def test_unchecked_jira_link_is_not_missing_or_unreadable(cfg):
    ev = {"kind": "jira", "ok": False, "url": "https://j/1", "error": None}
    r = v.compute(agg(), [ev], cfg, NOW)
    assert not r["missing_link"] and r["ball"] == "unknown" and "by hand" in r["next_action"]


def test_ai_handoff_is_our_turn(cfg):
    c = case(waiting_party=None, waiting_raw=None, links=[],
             ai={"state": "handed_off", "outcome": "handover", "url": "/runs/1"})
    r = v.compute(c, [], cfg, NOW)
    assert r["ball"] == "us" and not r["mismatch"] and r["reasons"][0]["source"]["url"] == "/runs/1"


def test_ai_working_is_ais_turn(cfg):
    c = case(waiting_party=None, links=[], ai={"state": "working"})
    assert v.compute(c, [], cfg, NOW)["ball"] == "ai"


def test_engineer_stale_threshold(cfg):
    c = case(last_modified_at=ago(20))
    r = v.compute(c, [gh(msg(True, 9))], cfg, NOW)
    assert r["stale"] and "Nudge Engineering" in r["next_action"]
    assert not v.compute(c, [gh(msg(True, 7))], cfg, NOW)["stale"]


def test_third_party_default_and_override(cfg):
    assert v.threshold_hours(agg(third_parties=["PartnerC"]), cfg) == 24
    assert v.threshold_hours(agg(third_parties=["partnera"]), cfg) == 12


def test_smallest_threshold_wins(cfg):
    assert v.threshold_hours(agg(third_parties=["PartnerA", "PartnerB", "PartnerC"]), cfg) == 6


def test_overdue_scores_base_plus_daily_and_caps(cfg):
    day = dict(target_at=ago(24 * 5), links=[{"kind": "github"}])
    assert v.compute(case(**day), [gh(msg(True))], cfg, NOW)["score"] == 40
    far = dict(target_at=ago(24 * 100))
    assert v.compute(case(**far), [gh(msg(True))], cfg, NOW)["score"] == 60


def test_future_target_is_not_overdue(cfg):
    r = v.compute(case(target_at=(NOW + timedelta(days=1)).isoformat()), [gh(msg(True))], cfg, NOW)
    assert not r["overdue"] and r["overdue_hours"] == 0


def test_reply_newer_than_case_update_adds_points(cfg):
    r = v.compute(case(last_modified_at=ago(5)), [gh(msg(False, 2))], cfg, NOW)
    assert r["score"] == 25 + 15


def test_old_reply_adds_no_new_reply_points(cfg):
    r = v.compute(case(last_modified_at=ago(1)), [gh(msg(False, 2))], cfg, NOW)
    assert "new_reply" not in codes(r)


@pytest.mark.parametrize("p,pts", [("Urgent", 10), ("High", 5), ("Medium", 0), (None, 0)])
def test_priority_points(cfg, p, pts):
    assert v.compute(case(priority=p), [gh(msg(True))], cfg, NOW)["score"] == pts


@pytest.mark.parametrize("score,level", [(60, "critical"), (59, "high"), (40, "high"),
                                         (39, "medium"), (20, "medium"), (19, "low"), (0, "low")])
def test_levels(score, level):
    assert v.score_level(score) == level


def test_combination_reaches_critical(cfg):
    c = case(priority="Urgent", last_reply_from="customer", last_modified_at=ago(30),
             target_at=ago(24))
    r = v.compute(c, [gh(msg(False, 10))], cfg, NOW)
    # overdue 32 + mismatch 25 + new reply 15 + stale 20 + customer 15 + urgent 10
    assert r["score"] == 117 and r["level"] == "critical"


def test_every_reason_has_a_source(cfg):
    c = case(priority="High", target_at=ago(30), last_reply_from="customer", links=[])
    r = v.compute(c, [{"kind": "github", "ok": False, "url": "u", "error": "e"}], cfg, NOW)
    assert all(x["source"]["label"] for x in r["reasons"])


def waiting_cfg(cfg):
    return {**cfg, "customer_wait_sub_statuses": ["Awaiting Revert"],
            "team_replied_sub_statuses": ["Internal Team Replied", "External Team Replied"]}


def test_solution_given_waits_on_merchant_not_us(cfg):
    c = case(sub_status="Awaiting Revert", last_modified_at=ago(1))
    r = v.compute(c, [gh(msg(False, 5))], waiting_cfg(cfg), NOW)
    assert r["ball"] == "customer" and not r["mismatch"]
    assert "merchant" in r["next_action"]


def test_closed_issue_doesnt_override_a_merchant_wait(cfg):
    c = case(sub_status="Awaiting Revert", last_modified_at=ago(1))
    assert v.compute(c, [gh(msg(False, 5), state="closed")], waiting_cfg(cfg), NOW)["ball"] == "customer"


def test_engineer_reply_after_the_update_is_still_our_turn(cfg):
    c = case(sub_status="Awaiting Revert", last_modified_at=ago(5))
    r = v.compute(c, [gh(msg(False, 2))], waiting_cfg(cfg), NOW)
    assert r["ball"] == "us" and r["mismatch"]


def test_merchant_reply_ends_the_merchant_wait(cfg):
    c = case(sub_status="Awaiting Revert", last_reply_from="customer")
    assert v.compute(c, [gh(msg(False, 5))], waiting_cfg(cfg), NOW)["ball"] == "us"


@pytest.mark.parametrize("sub", ["Internal Team Replied", "External Team Replied"])
def test_team_replied_sub_status_is_our_turn(cfg, sub):
    r = v.compute(case(sub_status=sub), [gh(msg(True))], waiting_cfg(cfg), NOW)
    assert r["ball"] == "us" and r["mismatch"] and "team_replied" in codes(r)


def test_email_reply_on_aggregator_case_is_our_turn(cfg):
    ev = {"kind": "email", "ok": True, "url": "email:1", "messages": [msg(False, 2, "ops@x.in")]}
    r = v.compute(agg(links=[]), [ev], cfg, NOW)
    assert r["ball"] == "us" and not r["missing_link"]


def test_email_with_our_message_last_waits_on_aggregator(cfg):
    ev = {"kind": "email", "ok": True, "url": "email:1", "messages": [msg(True, 2)]}
    assert v.compute(agg(links=[]), [ev], cfg, NOW)["ball"] == "third_party"


def test_sub_status_is_ignored_on_the_raising_record_type(cfg):
    conf = {**waiting_cfg(cfg), "dependency_record_type": "DSP"}
    c = agg(sub_status="Awaiting Revert", record_type="DSP")
    assert v.compute(c, [slack(msg(False, 5))], conf, NOW)["ball"] == "us"
