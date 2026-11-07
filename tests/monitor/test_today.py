from datetime import datetime, timedelta, timezone

from interpreter.monitor import today

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
CFG = {"issue_chase_hours": 4, "issue_quiet_hours": 24}
COACH = {"share": 0}
NO_SLA = {"partners": [], "total": 0, "breached": 0, "escalate": 0, "on_track": 0}
NO_TEAM = {"total": 0, "owners": []}


def ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


def issue(status="cause_given", waiting_on="tech", age=48, quiet=1, **kw):
    return {"issue": "8700", "url": "https://x/8700", "title": "Printer down", "squad": "menu-squad",
            "status": status, "waiting_on": waiting_on, "reason": "Printer removed", "opened_at": ago(age),
            "last_activity_at": ago(quiet), "case_id": "c1", "case_number": "42", "priority": "High", **kw}


def build(*rows, sla=NO_SLA, team=NO_TEAM, reviews=None, open_cases=10):
    return today.build({"squads": [{"issues": list(rows)}]}, sla, COACH, team, open_cases, reviews or {}, CFG, NOW)


def kinds(got):
    return [c["kind"] for c in got["cards"]]


def test_engineers_answering_comes_first():
    got = build(issue(status="no_reply", waiting_on="tech", age=10, issue="1"),
                issue(status="cause_given", waiting_on="support", issue="2"))
    assert kinds(got) == ["act", "chase"]


def test_no_reply_is_chased_only_after_the_limit():
    assert kinds(build(issue(status="no_reply", age=3))) == []
    got = build(issue(status="no_reply", age=5))
    assert kinds(got) == ["chase"] and "#8700" in got["cards"][0]["draft"]


def test_engineers_working_recently_need_nothing():
    assert kinds(build(issue(quiet=2))) == []


def test_engineers_going_quiet_are_chased():
    assert kinds(build(issue(quiet=30))) == ["chase"]


def test_a_finished_issue_is_offered_for_closing():
    assert kinds(build(issue(status="answered", waiting_on="nobody"))) == ["close"]


def test_waiting_on_a_merchant_only_shows_once_quiet():
    assert kinds(build(issue(waiting_on="merchant", quiet=2))) == []
    assert kinds(build(issue(waiting_on="merchant", quiet=30))) == ["follow"]


def test_an_unread_issue_is_never_shown_as_finished():
    assert kinds(build(issue(status="not_read", waiting_on=None))) == []


def late(partner="PartnerA", urgent=True):
    row = {"case_id": "c2", "subject": "Store down", "state": "breached", "urgent": urgent, "escalate_at": ago(3)}
    return {**NO_SLA, "partners": [{"name": partner, "late": [row], "draft": "Hi", "to": ["a@b.test"], "cc": [],
                                    "channel": "email", "contacts": [], "note": None}]}


def test_late_cases_become_one_card_per_partner():
    card = build(sla=late())["cards"][0]
    assert (card["kind"], card["title"], card["chip"]) == ("escalate", "Escalate 1 PartnerA case", "Urgent")
    assert card["to"] == ["a@b.test"]


def test_urgent_escalations_outrank_chasing_and_others_do_not():
    chase = issue(status="no_reply", age=5)
    assert kinds(build(chase, sla=late())) == ["escalate", "chase"]
    assert kinds(build(chase, sla=late(urgent=False))) == ["chase", "escalate"]


def test_a_partner_with_nothing_late_has_no_card():
    assert kinds(build(sla={**NO_SLA, "partners": [{"name": "P", "late": []}]})) == []


def test_done_hides_a_card_until_something_new_happens():
    row = issue(status="no_reply", age=5, quiet=5)
    done = {"gh:8700": {"reviewed_at": ago(1)}}
    got = build(row, reviews=done)
    assert (kinds(got), got["done"]) == ([], 1)
    assert kinds(build({**row, "last_activity_at": ago(0.5)}, reviews=done)) == ["chase"]


def test_done_never_hides_a_card_for_more_than_a_day():
    row = issue(status="no_reply", age=50, quiet=50)
    assert kinds(build(row, reviews={"gh:8700": {"reviewed_at": ago(25)}})) == ["chase"]


def test_the_clear_count_is_open_cases_with_no_card():
    assert build(issue(status="no_reply", age=5), sla=late())["clear"] == 8


def test_team_follow_ups_are_one_card():
    got = build(team={"total": 12, "owners": [{}, {}]})
    assert got["cards"][0]["title"] == "Send 2 agents their follow-ups"


def test_the_lane_numbers():
    got = build(issue(status="no_reply", age=5))
    assert got["github"] == {"open": 1, "need_you": 1, "with_tech": 1}


def test_a_help_request_comes_before_everything_else():
    view = {"cards": [{"key": "help:1", "kind": "help", "case_ids": [], "activity_at": ago(1)}],
            "away": {"total": 1, "answered": 0}, "modules": [("Order Relay", 3)]}
    got = today.build({"squads": [{"issues": [issue(status="cause_given", waiting_on="support")]}]}, NO_SLA, COACH,
                      NO_TEAM, 10, {}, CFG, NOW, view)
    assert kinds(got) == ["help", "act"]
    assert got["help"] == {"away": {"total": 1, "answered": 0}, "modules": [("Order Relay", 3)]}
