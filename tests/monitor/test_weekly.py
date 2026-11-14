from datetime import datetime, timedelta, timezone

from interpreter.monitor import weekly

NOW = datetime(2026, 11, 13, 6, tzinfo=timezone.utc)  # a Friday
CFG = {"timezone": "Asia/Kolkata", "week_start_weekday": 2}


def issue(status="no_reply", waiting_on="tech", case_id="c1", **kw):
    return {"issue": "8700", "title": "Orders stuck", "url": "https://x/8700", "squad": "order-squad", "status": status,
            "waiting_on": waiting_on, "case_id": case_id, "case_number": "42", "days_open": 3,
            "tech_status": "No tech reply", "businesses": [{"business": "Biz One", "biz_id": "1"}], **kw}


def tech(*rows):
    return {"squads": [{"squad": "any", "issues": list(rows)}]}


def eng(*rows):
    return weekly.engineering(tech(*rows), "Wed 11 Nov")


def test_the_week_starts_on_wednesday_in_the_leads_timezone():
    assert weekly.week_start(NOW, CFG).date().isoformat() == "2026-11-11"
    wed_early = datetime(2026, 11, 10, 19, tzinfo=timezone.utc)  # already Wednesday in India
    assert weekly.week_start(wed_early, CFG).date().isoformat() == "2026-11-11"


def test_an_issue_counts_only_while_tech_owes_the_resolution():
    for status in ("no_reply", "investigating", "cause_given"):
        assert eng(issue(status=status))["total"] == 1, status


def test_an_issue_with_no_open_case_is_left_out():
    got = eng(issue(case_id=None))
    assert got["total"] == 0 and got["left_out"] == {"No open case": 1}


def test_an_issue_waiting_on_support_is_left_out():
    got = eng(issue(status="cause_given", waiting_on="support"))
    assert got["total"] == 0 and got["left_out"] == {"Waiting on support": 1}


def test_an_issue_waiting_on_a_merchant_or_partner_is_left_out():
    assert eng(issue(status="investigating", waiting_on="merchant"))["left_out"] == {"Waiting on the merchant": 1}
    assert eng(issue(status="cause_given", waiting_on="third_party"))["left_out"] == {"Waiting on a partner": 1}


def test_answered_and_unread_issues_are_never_sent_to_tech():
    assert eng(issue(status="answered", waiting_on="nobody"))["left_out"] == {"Already answered": 1}
    assert eng(issue(status="not_read", waiting_on=None))["left_out"] == {"Not read yet": 1}


def test_the_message_groups_by_squad_with_the_case_and_business():
    got = eng(issue(), issue(squad="menu-squad"), issue(squad="menu-squad", issue="8701"))
    assert [s["squad"] for s in got["squads"]] == ["menu-squad", "order-squad"]
    assert "3 GitHub issues" in got["message"] and "menu-squad (2)" in got["message"]
    assert "#8700 Orders stuck | Case 42 | Biz One | 3 days open" in got["message"]
    assert "order-squad" not in got["squads"][0]["message"]


def test_nothing_owed_means_no_message():
    assert eng()["message"] is None


def row(state="breached", waiting=True, partner="PartnerA", **kw):
    return {"state": state, "waiting": waiting, "partner": partner, "case_number": "42", "subject": "Menu wrong",
            "business": "Biz One", "biz_id": "1", "raised_at": (NOW - timedelta(days=9)).isoformat(),
            "last_update_at": None, **kw}


def test_dsp_lists_cases_past_sla_that_are_waiting_on_them():
    got = weekly.dsp({"rows": [row(), row(state="on_track"), row(waiting=False)]}, NOW)
    assert got["total"] == 1
    assert "9 days without an update" in got["partners"][0]["message"]


def test_quiet_days_count_from_the_last_update():
    got = weekly.dsp({"rows": [row(last_update_at=(NOW - timedelta(days=2)).isoformat())]}, NOW)
    assert got["partners"][0]["rows"][0]["quiet_days"] == 2


def test_an_aggregator_that_replied_today_is_not_no_response():
    got = weekly.dsp({"rows": [row(last_update_at=(NOW - timedelta(hours=5)).isoformat())]}, NOW)
    assert got["total"] == 0


def test_the_report_has_both_parts():
    got = weekly.build(tech(issue()), {"rows": [row()]}, NOW, CFG)
    assert got["engineering"]["total"] == 1 and got["dsp"]["total"] == 1
    assert "Fri 13 Nov" in got["engineering"]["message"]
    assert got["week_label"] == "Wed 11 Nov"
