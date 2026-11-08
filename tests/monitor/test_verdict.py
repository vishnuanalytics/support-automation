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


def test_internal_thread_cant_show_a_third_partys_turn(cfg):
    ev = {**slack(msg(True, 2)), "internal": True}
    r = v.compute(agg(last_modified_at=ago(100)), [ev], cfg, NOW)
    assert r["ball"] == "unknown" and r["stale_hours"] == 2


def test_directory_escalation_time_counts_toward_the_smallest_threshold(cfg):
    c = agg(third_parties=["Other"], partner={"escalate_after_hours": 72})
    assert v.threshold_hours(c, {**cfg, "third_party_nudge_hours": 24}) == 72


def test_stale_action_includes_the_escalation_path(cfg):
    conf = {**cfg, "third_party_escalation": "Use the escalation channel."}
    c = agg(last_modified_at=ago(100))
    r = v.compute(c, [slack(msg(True, 50))], conf, NOW)
    assert r["next_action"].endswith("Use the escalation channel.")


@pytest.mark.parametrize("flags,want", [
    ({"ball": "us", "not_raised": True}, "act"),
    ({"ball": "unknown", "not_raised": True}, "raise"),
    ({"ball": "unknown", "missing_link": True}, "raise"),
    ({"ball": "engineering"}, "engineering"),
    ({"ball": "third_party"}, "third_party"),
    ({"ball": "customer"}, "customer"),
    ({"ball": "ai"}, "ai"),
    ({"ball": "unknown"}, "check"),
])
def test_every_case_has_exactly_one_stage(flags, want):
    assert v.stage(flags) == want


def test_verdict_reports_the_signal_and_newest_message(cfg):
    r = v.compute(case(), [gh(msg(True, 9), msg(False, 2, "dev"))], cfg, NOW)
    assert r["signal"]["code"] == "engineer_replied"
    assert r["latest"]["who"] == "dev" and r["latest"]["url"] == "https://src/1"


def test_no_evidence_has_no_signal_or_latest(cfg):
    r = v.compute(case(links=[]), [], cfg, NOW)
    assert r["signal"] is None and r["latest"] is None


PARTNER_CFG = {"internal_email_domains": ["us.test"]}
ACME_CASE = {"partner": {"name": "Acme Eats", "email_domains": ["acme.test"]},
             "third_parties": ["Acme Eats", "NA"]}


@pytest.mark.parametrize("sender,want", [
    ("agent@us.test", "us"),
    ("ops@acme.test", "third_party"),
    ("Ops <ops@external.acme.test>", "third_party"),
    ("help@acmeeats.io", "third_party"),
    ("owner@gmail.com", "customer"),
    ("gm@bigbrand.in", "customer"),
    ("x@notacme.test", "customer"),
    ("x@some-na.test", "customer"),
])
def test_email_party_from_sender_domain(sender, want):
    assert v.email_party(sender, True, ACME_CASE, PARTNER_CFG) == want


def test_outgoing_email_is_always_ours():
    assert v.email_party("anything@x.test", False, ACME_CASE, PARTNER_CFG) == "us"


def test_merchant_looped_into_the_thread_is_our_turn_and_says_so(cfg):
    m = {**msg(False, 2, "owner@gmail.com"), "party": "customer"}
    ev = {"kind": "email", "ok": True, "url": "e", "messages": [msg(True, 5), m]}
    r = v.compute(agg(links=[]), [ev], cfg, NOW)
    assert r["ball"] == "us" and r["signal"]["code"] == "customer_replied_email"
    assert "Merchant" in r["signal"]["text"] and r["latest"]["party"] == "customer"


def test_latest_party_defaults_by_source():
    assert v.latest_message([gh(msg(False))])["party"] == "engineering"
    assert v.latest_message([slack(msg(False))])["party"] == "third_party"
    assert v.latest_message([gh(msg(True))])["party"] == "us"


def test_reply_field_is_ignored_on_the_raising_record_type(cfg):
    conf = {**cfg, "dependency_record_type": "DSP"}
    ev = {"kind": "email", "ok": True, "url": "e", "messages": [msg(True, 2)]}
    c = agg(links=[], record_type="DSP", last_reply_from="customer")
    r = v.compute(c, [ev], conf, NOW)
    assert r["ball"] == "third_party" and "customer_replied" not in codes(r)


def resolved_dsp_child(**kw):
    """A Support case whose parent DSP case (the one raised with the aggregator) is closed."""
    parent = {"closed": True, "record_type": "DSP", "case_number": "900", "status": "Resolved",
              "url": "https://sf/900"}
    return agg(record_type="Support", parent=parent, **kw)


def test_a_resolved_dsp_parent_makes_the_case_ready_to_close(cfg):
    conf = {**cfg, "dependency_record_type": "DSP"}
    r = v.compute(resolved_dsp_child(), [], conf, NOW)
    assert r["ball"] == "us" and r["ready_to_close"]
    assert v.stage(r) == "close"
    assert r["next_action"] == "Update the merchant and close this case."
    # The waiting field naming the finished dependency is expected, not a mismatch.
    assert not r["mismatch"] and "mismatch" not in {x["code"] for x in r["reasons"]}


def test_ready_to_close_still_counts_overdue(cfg):
    conf = {**cfg, "dependency_record_type": "DSP"}
    r = v.compute(resolved_dsp_child(target_at=ago(24 * 30)), [], conf, NOW)
    assert r["ready_to_close"] and r["overdue"]
    assert "overdue" in {x["code"] for x in r["reasons"]}


def test_an_open_dsp_parent_is_not_ready_to_close(cfg):
    conf = {**cfg, "dependency_record_type": "DSP"}
    c = resolved_dsp_child()
    c["parent"] = {**c["parent"], "closed": False, "status": "In Progress"}
    r = v.compute(c, [], conf, NOW)
    assert not r["ready_to_close"] and v.stage(r) != "close"


def test_a_customer_reply_outranks_ready_to_close(cfg):
    """If the merchant wrote back, someone must read it before closing."""
    conf = {**cfg, "dependency_record_type": "DSP"}
    r = v.compute(resolved_dsp_child(last_reply_from="customer"), [], conf, NOW)
    assert r["signal"]["code"] == "customer_replied"
    assert not r["ready_to_close"] and v.stage(r) == "act"


def test_ready_to_close_has_its_own_stage():
    assert v.stage({"ball": "us", "ready_to_close": True}) == "close"


def merchant_said(kind, at=None, summary=""):
    return {"kind": kind, "summary": summary, "at": at or ago(1)}


def test_a_merchant_confirmation_makes_the_case_ready_to_close(cfg):
    c = agg(last_reply_from="customer", customer_reply=merchant_said("confirmed", summary="Images show now"))
    r = v.compute(c, [slack(msg(True, 5))], cfg, NOW)
    assert r["ball"] == "us" and r["ready_to_close"] and v.stage(r) == "close"
    assert r["signal"]["code"] == "customer_confirmed" and r["signal"]["text"] == "Images show now"
    assert "customer_replied" not in {x["code"] for x in r["reasons"]}


def test_a_thank_you_alone_doesnt_make_it_our_turn(cfg):
    c = agg(last_reply_from="customer", customer_reply=merchant_said("thanks"))
    r = v.compute(c, [slack(msg(True, 5))], cfg, NOW)
    assert r["ball"] == "third_party" and not r["ready_to_close"]
    codes = {x["code"]: x["points"] for x in r["reasons"]}
    assert codes.get("customer_thanks") == 0 and "customer_replied" not in codes


@pytest.mark.parametrize("label", [None, "needs_reply"])
def test_a_merchant_reply_without_a_settling_label_still_needs_us(cfg, label):
    extra = {"customer_reply": merchant_said(label)} if label else {}
    r = v.compute(agg(last_reply_from="customer", **extra), [slack(msg(True, 5))], cfg, NOW)
    assert r["ball"] == "us" and r["signal"]["code"] == "customer_replied"
    assert {x["code"]: x["points"] for x in r["reasons"]}["customer_replied"] == 15


def _merchant_email(at):
    return {"kind": "email", "ok": True, "url": "email:1", "label": "DSP email",
            "messages": [{"who": "owner@shop.test", "ours": False, "party": "customer", "at": at,
                          "text": "Its working now"}]}


def test_a_labelled_merchant_email_on_the_thread_is_read(cfg):
    at = ago(1)
    c = agg(customer_reply=merchant_said("confirmed", at=at))
    r = v.compute(c, [_merchant_email(at)], cfg, NOW)
    assert r["signal"]["code"] == "customer_confirmed" and r["ready_to_close"]


def test_a_label_for_a_different_message_is_not_applied(cfg):
    c = agg(customer_reply=merchant_said("confirmed", at=ago(30)))
    r = v.compute(c, [_merchant_email(ago(1))], cfg, NOW)
    assert r["signal"]["code"] == "customer_replied_email" and not r["ready_to_close"]


def _dsp(**kw):
    return case(waiting_party="third_party", waiting_raw="Aggregator", record_type="DSP",
                reply={"kind": "needs_info", "summary": "They cannot find the items."},
                links=[{"kind": "slack"}], **kw)


def test_dsp_third_party_ask_is_our_turn_without_a_linked_wait(cfg):
    r = v.compute(_dsp(), [slack(msg(False, who="zomato"))], {**cfg, "dependency_record_type": "DSP"}, NOW)
    assert r["ball"] == "us" and r["signal"]["code"] == "reply_needs_info" and r["mismatch"]


def test_dsp_waits_on_the_merchant_when_the_linked_support_case_does(cfg):
    lw = {"id": "2", "case_number": "00000043", "record_type": "Support", "url": "https://sf/2"}
    r = v.compute(_dsp(linked_wait=lw), [slack(msg(False, who="zomato"))],
                  {**cfg, "dependency_record_type": "DSP"}, NOW)
    assert r["ball"] == "customer" and not r["mismatch"] and v.stage(r) == "customer"
    assert r["signal"]["code"] == "linked_awaiting_customer"
    assert r["signal"]["source"]["url"] == "https://sf/2"
    assert r["next_action"] == "Chase the merchant on case 00000043, then answer Aggregator."


def _dsp_ref(number, closed, via=None):
    ref = {"id": number, "case_number": number, "record_type": "DSP", "closed": closed,
           "status": "Resolved" if closed else "In Progress", "url": f"https://sf/{number}"}
    return {**ref, "via": via} if via else ref


def test_a_closed_subject_linked_dsp_child_makes_the_case_ready_to_close(cfg):
    conf = {**cfg, "dependency_record_type": "DSP"}
    c = agg(record_type="Support", parent=None, children=[_dsp_ref("00132973", True, "subject")])
    r = v.compute(c, [], conf, NOW)
    assert r["ready_to_close"] and r["signal"]["code"] == "dsp_resolved"
    assert r["signal"]["text"] == "Linked DSP case 00132973 is Resolved"
    assert r["signal"]["source"]["url"] == "https://sf/00132973"


def test_one_aggregator_closed_while_another_is_open_is_not_ready_to_close(cfg):
    conf = {**cfg, "dependency_record_type": "DSP"}
    c = agg(record_type="Support", parent=_dsp_ref("00132972", False),
            children=[_dsp_ref("00132973", True, "subject")])
    assert not v.compute(c, [], conf, NOW)["ready_to_close"]
    c["parent"] = _dsp_ref("00132972", True)
    r = v.compute(c, [], conf, NOW)
    assert r["ready_to_close"] and r["signal"]["text"] == "Linked DSP cases 00132972, 00132973 are all closed"


def test_a_dsp_child_with_no_closed_state_counts_as_open(cfg):
    """Rows saved before children carried IsClosed: don't call them done."""
    conf = {**cfg, "dependency_record_type": "DSP"}
    child = {k: val for k, val in _dsp_ref("00132973", True).items() if k != "closed"}
    c = agg(record_type="Support", parent=_dsp_ref("900", True), children=[child])
    assert not v.compute(c, [], conf, NOW)["ready_to_close"]


def test_non_dsp_children_dont_block_ready_to_close(cfg):
    conf = {**cfg, "dependency_record_type": "DSP"}
    c = resolved_dsp_child(children=[{"id": "x", "case_number": "1", "record_type": "Support"}])
    assert v.compute(c, [], conf, NOW)["ready_to_close"]
