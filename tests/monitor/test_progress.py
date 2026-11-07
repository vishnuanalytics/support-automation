from datetime import datetime, timedelta, timezone

from interpreter.monitor import progress as pr

NOW = datetime(2026, 11, 7, 20, tzinfo=timezone.utc)  # already Nov 8 in the lead's zone
CFG = {"assist_hours": {"tz": "Asia/Kolkata"}}


def today(cards=2, done=1):
    return {"cards": [{}] * cards, "done": done, "open_cases": 100, "github": {"open": 21},
            "coaching": {"share": 45}}


def snap(sla=None, closed=(), reviews=(), previous=None, **kw):
    return pr.snapshot(today(**kw), sla or {"rows": []}, list(closed), list(reviews), previous, NOW, CFG)


def test_the_day_is_the_leads_own():
    assert pr.local_day(NOW, CFG) == "2026-11-08" and pr.local_day(NOW, {}) == "2026-11-07"


def test_cards_keep_the_busiest_point_of_the_day():
    assert snap()["cards"] == 3
    assert snap(previous={"cards": 19})["cards"] == 19
    assert snap(previous={"cards": 1})["cards"] == 3


def test_only_cards_done_today_are_counted():
    reviews = [{"case_id": "gh:o/r#1", "reviewed_at": NOW.isoformat()},
               {"case_id": "dsp:PartnerA", "reviewed_at": NOW.isoformat()},
               {"case_id": "500abc", "reviewed_at": NOW.isoformat()},  # a reviewed case, not a Today card
               {"case_id": "gh:o/r#2", "reviewed_at": (NOW - timedelta(days=2)).isoformat()}]
    assert snap(reviews=reviews)["done"] == 2


def test_github_close_time_is_the_weeks_median():
    def closed(days_ago, hours):
        done = NOW - timedelta(days=days_ago)
        return {"closed_at": done.isoformat(), "opened_at": (done - timedelta(hours=hours)).isoformat()}
    assert snap(closed=[closed(1, 10), closed(2, 20), closed(3, 90), closed(9, 500)])["github_close_hours"] == 20
    assert snap()["github_close_hours"] is None


def test_new_and_old_late_cases_stay_apart():
    rows = [{"new": True, "state": "breached"}, {"new": False, "state": "breached"},
            {"new": False, "state": "breached"}, {"new": True, "state": "on_track"}]
    got = snap(sla={"rows": rows})
    assert (got["dsp_new_late"], got["dsp_old_late"]) == (1, 2)


def rows(*cards):
    return [{"day": f"2026-11-{i + 1:02d}", "stats": {"cards": c, "done": c}} for i, c in enumerate(cards)]


def measure(got, key):
    return next(m for m in got["measures"] if m["key"] == key)


def test_no_rows_means_nothing_to_show():
    assert pr.summary([])["measures"] == []


def test_a_change_waits_for_more_than_a_week_of_data():
    got = measure(pr.summary(rows(20, 10)), "cards")
    assert (got["start"], got["now"], got["change"]) == (15, 15, "too_early")


def test_the_first_week_is_compared_with_the_latest_week():
    data = pr.summary(rows(20, 20, 20, 20, 20, 20, 20, 10, 10, 10, 10, 10, 10, 10))
    cards, done = measure(data, "cards"), measure(data, "done")
    assert (cards["start"], cards["now"], cards["change"]) == (20, 10, "better")
    assert done["change"] == "not_judged"  # fewer done can just mean fewer things to do


def test_a_measure_with_no_numbers_stays_empty():
    got = measure(pr.summary(rows(20)), "github_close_hours")
    assert (got["start"], got["now"], got["change"]) == (None, None, "too_early")
