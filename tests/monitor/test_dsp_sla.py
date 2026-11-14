from datetime import datetime, timedelta, timezone

from interpreter.monitor import dsp_sla as sla

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
STORE = {"name": "Store offline", "modules": ["Stores"], "keywords": ["store is closed"],
         "escalate_hours": 2, "resolve_hours": 4}
OLD = {"name": "Old order", "sub_modules": ["Value mismatch"], "escalate_hours": 72, "resolve_hours": 168}
CFG = {"dsp_sla_rules": [STORE, OLD], "dsp_escalate_share": 0.5}
WAITING = {"ball": "third_party", "signal": {}, "level": "low"}


def ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


def case(hours=1.0, **kw):
    return {"id": "1", "case_number": "42", "subject": "Store down", "module": "Stores", "owner": "Asha",
            "third_parties": ["PartnerA"], "created_at": ago(hours), **kw}


def update(hours, text="The outlet is offline due to high rejections"):
    return {"kind": "email", "ok": True, "messages": [{"who": "ops", "at": ago(hours), "party": "third_party",
                                                       "text": text}]}


def row(c, evidence=(), v=None):
    return sla.row(c, v or WAITING, list(evidence), NOW, CFG)


def test_a_store_issue_is_on_track_at_first():
    r = row(case(1))
    assert (r["rule"], r["state"], r["urgent"]) == ("Store offline", sla.ON_TRACK, True)


def test_two_hours_with_no_update_means_escalate():
    assert row(case(2.5))["state"] == sla.ESCALATE


def test_an_acknowledgement_is_not_an_update():
    assert row(case(2.5), [update(2, "Looking into it")])["state"] == sla.ESCALATE


def test_a_real_update_stops_the_escalation_until_the_deadline():
    assert row(case(2.5), [update(1)])["state"] == sla.ON_TRACK


def test_past_four_hours_is_breached_even_with_an_update():
    assert row(case(5), [update(1)])["state"] == sla.BREACHED


def test_an_old_order_issue_has_seven_days():
    r = row(case(100, module="Orders", sub_module="Value mismatch"))
    assert (r["rule"], r["urgent"]) == ("Old order", False)
    assert r["state"] == sla.ESCALATE


def test_a_keyword_in_the_subject_also_matches():
    assert row(case(1, module="Other", subject="Store is CLOSED on app"))["rule"] == "Store offline"


def test_with_no_rule_the_case_target_date_sets_the_clock():
    c = case(30, module="Menu", subject="Image", target_at=ago(-10))
    r = row(c)
    assert (r["rule"], r["state"]) == (sla.STANDARD, sla.ESCALATE)
    assert row(case(1, module="Menu", subject="Image", target_at=ago(-10)))["state"] == sla.ON_TRACK


def test_a_case_with_no_clock_is_skipped():
    assert row(case(1, module="Menu", subject="Image")) is None
    assert row(case(1, created_at=None)) is None


def build(*cases):
    return sla.build([(c, WAITING, []) for c in cases], NOW, CFG)


def test_late_cases_group_into_one_escalation_per_partner():
    a = case(5, partner={"to_emails": ["ops@a.test"], "cc_emails": ["cc@a.test"], "escalation_note": "Ping lead"})
    b = case(2.5, id="2", case_number="43", subject="Another store")
    other = case(1, id="3", case_number="44", third_parties=["PartnerB"])
    got = build(a, b, other)
    assert (got["total"], got["breached"], got["escalate"], got["on_track"]) == (3, 1, 1, 1)
    p = got["partners"][0]
    assert (p["name"], p["to"], p["note"]) == ("PartnerA", ["ops@a.test"], "Ping lead")
    assert "42: Store down" in p["draft"] and "43: Another store" in p["draft"]
    assert got["partners"][1]["draft"] is None


def test_a_case_that_is_our_turn_is_not_escalated_to_the_partner():
    got = sla.build([(case(5), {"ball": "act", "signal": {}, "level": "low"}, [])], NOW, CFG)
    assert got["breached"] == 1 and got["partners"][0]["late"] == []


def test_the_weekly_report_counts_by_partner():
    resolved = [{"third_parties": ["PartnerA"], "module": "Stores", "opened_at": ago(10), "closed_at": ago(8)},
                {"third_parties": ["PartnerA"], "module": "Stores", "opened_at": ago(30), "closed_at": ago(20)},
                {"third_parties": ["PartnerA"], "module": "Stores", "opened_at": ago(900), "closed_at": ago(800)},
                {"third_parties": ["PartnerA"], "module": "Menu", "opened_at": ago(90), "closed_at": ago(8)},
                {"third_parties": [], "module": "Stores", "opened_at": ago(10), "closed_at": ago(8)}]
    got = sla.weekly_report(build(case(5)), resolved, NOW, CFG)
    assert got["partners"] == [{"name": "PartnerA", "open": 1, "open_late": 1, "old_open": 0, "old_late": 0,
                                "closed": 3, "closed_timed": 2, "closed_in_time": 1}]
    assert got["rules"][0]["name"] == "Store offline" and (got["rules"][0]["closed"], got["rules"][0]["in_time"]) == (3, 2)


def stores(n_in_time, n_late):
    return ([{"module": "Stores", "opened_at": ago(10), "closed_at": ago(8)}] * n_in_time
            + [{"module": "Stores", "opened_at": ago(20), "closed_at": ago(8)}] * n_late)


def rule_after(resolved, **rule):
    cfg = {**CFG, "dsp_sla_rules": [{**STORE, **rule}]}
    return sla.weekly_report(build(), resolved, NOW, cfg)["rules"][0]


def test_a_rule_is_ready_to_tighten_once_most_cases_beat_it():
    got = rule_after(stores(4, 1), goal_resolve_hours=2)
    assert (got["share"], got["ready"]) == (80, True)


def test_a_rule_needs_enough_cases_before_tightening():
    assert rule_after(stores(4, 0), goal_resolve_hours=2)["ready"] is False


def test_a_rule_below_the_share_is_not_ready():
    assert rule_after(stores(3, 2), goal_resolve_hours=2)["ready"] is False


def test_a_rule_at_its_goal_stops_tightening():
    assert rule_after(stores(5, 0), goal_resolve_hours=4)["ready"] is False


def test_span_reads_naturally():
    assert (sla.span(0.5), sla.span(5.9), sla.span(50), sla.span(-3)) == ("30m", "5h", "2d 2h", "3h")


def test_a_row_carries_its_business():
    r = row(case(business_name="Biz One", biz_id="111"))
    assert (r["business"], r["biz_id"]) == ("Biz One", "111")


START = {**CFG, "sla_start": ago(20)}


def test_a_case_raised_before_the_start_is_old_backlog():
    got = sla.build([(case(30), WAITING, []), (case(5, id="2"), WAITING, [])], NOW, START)
    assert [r["new"] for r in got["rows"]] == [False, True]
    p = sla.weekly_report(got, [], NOW, START)["partners"][0]
    assert (p["open"], p["open_late"], p["old_open"], p["old_late"]) == (1, 1, 1, 1)


def test_with_no_start_every_case_is_new():
    assert row(case(900))["new"] is True


def test_old_closed_cases_are_not_judged_by_the_sla():
    resolved = [{"third_parties": ["PartnerA"], "module": "Stores", "opened_at": ago(30), "closed_at": ago(28)},
                {"third_parties": ["PartnerA"], "module": "Stores", "opened_at": ago(10), "closed_at": ago(8)}]
    got = sla.weekly_report(sla.build([], NOW, START), resolved, NOW, START)
    p = got["partners"][0]
    assert (p["closed"], p["closed_timed"], p["closed_in_time"]) == (2, 1, 1)
    assert (got["rules"][0]["closed"], got["rules"][0]["in_time"]) == (1, 1)
