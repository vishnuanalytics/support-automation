from datetime import datetime, timedelta, timezone

from interpreter.monitor import help_requests as hr

NOW = datetime(2026, 11, 11, 8, tzinfo=timezone.utc)  # 13:30 in the lead's zone
HOURS = {"start": 11, "end": 20, "tz": "Asia/Kolkata"}


def ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


def ask(hours=0.5, **kw):
    return {"ts": "1.1", "agent": "asha", "ticket": "42", "module": "Order Relay", "sub_module": "Discount",
            "priority": "High", "question": "Why is this doubled?", "url": "https://slack/x",
            "raised_at": ago(hours), **kw}


def build(*rows, cases=None):
    return hr.build(list(rows), HOURS, cases or {}, NOW)


def test_an_open_request_becomes_a_card_with_the_case():
    c = build(ask(), cases={"42": "case-id"})["cards"][0]
    assert (c["title"], c["case_id"], c["tone"]) == ("asha needs help on 42", "case-id", "warn")
    assert c["why"] == "Why is this doubled?" and c["url"] == "https://slack/x"


def test_waiting_over_an_hour_turns_red():
    assert build(ask(2))["cards"][0]["tone"] == "exception"


def test_an_answered_request_has_no_card():
    assert build(ask(answered_at=ago(0.1), answered_by="Afzal"))["cards"] == []


def test_an_old_request_stops_showing_but_still_counts_as_a_topic():
    got = build(ask(24 * 10))
    assert got["cards"] == [] and got["modules"] == [("Order Relay", 1)]


def test_hours_use_the_leads_time_zone():
    assert hr.in_hours(datetime(2026, 11, 11, 6, tzinfo=timezone.utc), HOURS)  # 11:30 local
    assert not hr.in_hours(datetime(2026, 11, 11, 15, tzinfo=timezone.utc), HOURS)  # 20:30 local


def test_a_request_outside_your_hours_says_so_and_counts_towards_the_overnight_note():
    got = build(ask(12), ask(14, ts="1.2", answered_at=ago(13), answered_by="Saddam"))
    assert "Raised outside your hours" in got["cards"][0]["meta"]
    assert got["away"] == {"total": 2, "answered": 1}


def test_a_request_during_your_hours_is_not_in_the_overnight_note():
    assert build(ask(1))["away"] == {"total": 0, "answered": 0}


def test_no_hours_set_means_nothing_is_marked_away():
    got = hr.build([ask(12)], {}, {}, NOW)
    assert "outside" not in got["cards"][0]["meta"] and got["away"]["total"] == 0


def test_a_request_with_no_date_is_skipped():
    assert build(ask(raised_at=None))["cards"] == []
