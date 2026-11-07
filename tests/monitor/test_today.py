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


def build(*rows, sla=NO_SLA, team=NO_TEAM, reviews=None, open_cases=10, merchant_sent=None):
    return today.build({"squads": [{"issues": list(rows)}]}, sla, COACH, team, open_cases, reviews or {}, CFG, NOW,
                       merchant_sent=merchant_sent)


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


def test_team_follow_ups_stay_off_needs_me():
    assert build(team={"total": 12, "owners": [{}, {}]})["cards"] == []


def test_several_quiet_issues_become_one_card():
    got = build(issue(status="no_reply", age=5), issue(issue="8701", status="no_reply", age=9, case_id="c2"))
    assert [c["title"] for c in got["cards"]] == ["2 GitHub issues gone quiet"]
    assert got["cards"][0]["key"] == "gh:chase" and got["cards"][0]["case_ids"] == ["c1", "c2"]
    assert "- Printer down: https://x/8700" in got["cards"][0]["draft"]


def test_each_card_sits_in_one_of_three_groups():
    got = build(issue(waiting_on="support"), issue(issue="8701", status="no_reply", age=5),
                issue(issue="8702", waiting_on="merchant", quiet=30))
    assert [c["group"] for c in got["cards"]] == ["replies", "sla", "agents"]


REPLY_CFG = {"needs_reply_hours": 24, "dependency_record_type": "DSP"}


def waiting(owner="Asha", hours=30, party="third_party", ball="us", number="42", who=None, kind=None, record="Support"):
    case = {"id": number, "case_number": number, "subject": "Menu wrong", "owner": owner, "record_type": record,
            "third_parties": ["PartnerA"], "reply": {"kind": kind}}
    v = {"ball": ball, "latest": {"at": ago(hours), "party": party, "source": "DSP email", "who": who}}
    return case, v, []


def test_replies_nobody_acted_on_become_one_card_per_agent():
    cards = today.reply_cards([waiting(), waiting(number="43", hours=80), waiting(owner="Ravi", number="44")],
                              REPLY_CFG, NOW)
    assert [c["title"] for c in cards] == ["Asha: 2 replies not acted on", "Ravi: 1 reply not acted on"]
    assert cards[0]["why"].startswith("Oldest: 43") and cards[0]["tone"] == "exception"
    assert "- 43: Read the PartnerA reply and act on it (3d 8h ago)" in cards[0]["draft"]


def test_each_line_says_the_next_step_in_a_few_words():
    def step(**kw):
        return today.reply_cards([waiting(**kw)], REPLY_CFG, NOW)[0]["steps"]["42"]
    assert step(kind="no_answer") == "Push PartnerA for a real answer"
    assert step(kind="needs_info") == "Send PartnerA what they asked for"
    assert step(party="customer") == "Reply to the merchant"
    assert step(party="engineering") == "Read the engineer's reply and update the merchant"


def test_one_reply_on_two_linked_cases_is_one_job():
    cards = today.reply_cards([waiting(number="1", who="a@partner.test"), waiting(number="2", who="a@partner.test", record="DSP")],
                              REPLY_CFG, NOW)
    assert [c["title"] for c in cards] == ["Asha: 1 reply not acted on"]
    assert "- 2 + 1: " in cards[0]["draft"] and sorted(cards[0]["case_ids"]) == ["1", "2"]


def test_a_shared_reply_goes_to_the_aggregator_case_owner_and_names_the_other():
    cards = today.reply_cards([waiting(number="1", who="a@partner.test"),
                               waiting(owner="Ravi", number="2", who="a@partner.test", record="DSP")], REPLY_CFG, NOW)
    assert [c["title"] for c in cards] == ["Ravi: 1 reply not acted on"]
    assert "- 2 + 1 (Asha): " in cards[0]["draft"]


def test_a_fresh_reply_or_our_own_message_needs_nothing():
    assert today.reply_cards([waiting(hours=5), waiting(party="us")], REPLY_CFG, NOW) == []


def test_a_case_waiting_on_someone_else_needs_nothing():
    assert today.reply_cards([waiting(ball="third_party")], REPLY_CFG, NOW) == []


def test_a_github_reply_already_in_an_agents_card_shows_once():
    reply = today.reply_cards([waiting(number="c1")], REPLY_CFG, NOW)
    got = today.build({"squads": [{"issues": [issue(waiting_on="support", case_id="c1")]}]}, NO_SLA, COACH, NO_TEAM, 10,
                      {}, CFG, NOW, replies=reply)
    assert [c["kind"] for c in got["cards"]] == ["reply"]


def test_merchant_told_after_the_engineers_answer_means_follow_up_the_merchant():
    row = issue(status="cause_given", waiting_on="support", quiet=60)
    got = build(row, merchant_sent={"c1": ago(30)})
    assert [c["kind"] for c in got["cards"]] == ["follow"]
    assert got["cards"][0]["title"] == "#8700: follow up with the merchant, GitHub open"
    assert got["cards"][0]["group"] == "agents"


def test_a_merchant_just_told_stays_off_the_list_until_quiet():
    row = issue(status="cause_given", waiting_on="support", quiet=60)
    assert build(row, merchant_sent={"c1": ago(2)})["cards"] == []


def test_an_older_email_to_the_merchant_leaves_the_next_move_with_us():
    row = issue(status="cause_given", waiting_on="support", quiet=5)
    assert kinds(build(row, merchant_sent={"c1": ago(30)})) == ["act"]


def _mail(at, ours=True, to="owner@merchant.test"):
    return {"at": at, "ours": ours, "to": to}


def _item(*mails, domains=()):
    case = {"id": "c1", "partner": {"email_domains": list(domains)}}
    return case, {}, [{"label": "Case email", "messages": list(mails)}]


def test_our_last_email_counts_as_telling_the_merchant():
    assert today.merchant_emails([_item(_mail(ago(9)), _mail(ago(3)))]) == {"c1": ago(3)}


def test_a_merchant_reply_after_ours_or_an_email_to_the_aggregator_does_not():
    assert today.merchant_emails([_item(_mail(ago(9)), _mail(ago(3), ours=False))]) == {}
    assert today.merchant_emails([_item(_mail(ago(3), to="ops@partner.test"), domains=["partner.test"])]) == {}


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
