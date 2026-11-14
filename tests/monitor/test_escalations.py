from datetime import datetime, timedelta, timezone

from interpreter.monitor import escalations

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)


def ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def case(**kw):
    return {"id": "1", "case_number": "42", "subject": "Menu stuck", "record_type": "DSP",
            "owner": "Asha Rao", "third_parties": ["AcmeEats"], "created_at": ago(30), **kw}


def wait(ball="third_party", **kw):
    return {"ball": ball, "not_raised": False, "missing_link": False, "latest": None, **kw}


def email(days, party, text="The menu sync is fixed on our side now", ours=None):
    return {"at": ago(days), "who": f"{party}@x.com", "party": party, "text": text,
            "ours": party == "us" if ours is None else ours}


def ev(kind, *messages, ok=True, **kw):
    return {"kind": kind, "ok": ok, "messages": list(messages), **kw}


def row(c=None, v=None, evidence=()):
    return escalations.row(c or case(), v or wait(), list(evidence), 5, NOW)


def test_aggregator_silent_for_five_days_is_listed():
    got = row(evidence=[ev("email", email(9, "third_party"), email(2, "us"), email(7, "us"))])
    assert got["days_silent"] == 9 and got["last_update_via"] == "Email"
    assert got["our_follow_ups"] == 2 and got["our_last_at"] == ago(2)
    assert got["waiting_on"] == ["AcmeEats"]


def test_recent_update_on_any_channel_keeps_it_off():
    slack = ev("slack", {"at": ago(1), "who": "Partner", "ours": False, "text": "Root cause found, patching it"})
    assert row(evidence=[ev("email", email(9, "third_party")), slack]) is None


def test_exactly_five_days_counts():
    assert row(evidence=[ev("email", email(5, "third_party"))])["days_silent"] == 5
    assert row(evidence=[ev("email", email(4.9, "third_party"))]) is None


def test_never_replied_counts_from_our_first_mail():
    got = row(evidence=[ev("email", email(2, "us"), email(8, "us"))])
    assert got["last_update_at"] is None and got["days_silent"] == 8 and got["our_follow_ups"] == 2


def test_an_ack_is_not_an_update():
    got = row(evidence=[ev("email", email(9, "third_party"), email(1, "third_party", text="Noted, checking"))])
    assert got["days_silent"] == 9 and got["last_ack_at"] == ago(1)


def test_the_llm_ack_label_applies_to_the_latest_message_only():
    long_ack = email(1, "third_party")
    v = wait(latest={"at": long_ack["at"]})
    c = case(reply={"kind": "ack"})
    assert row(c, v, [ev("email", email(9, "third_party"), long_ack)])["days_silent"] == 9


def test_customers_and_internal_threads_are_not_the_aggregator():
    internal = ev("slack", {"at": ago(1), "who": "Colleague", "ours": False, "text": "Any news on this one?"},
                  internal=True)
    got = row(evidence=[ev("email", email(9, "third_party"), email(1, "customer")), internal])
    assert got["days_silent"] == 9


def test_our_turn_or_never_raised_is_not_an_escalation():
    old = [ev("email", email(9, "us"))]
    assert row(v=wait("us"), evidence=old) is None
    assert row(v=wait(not_raised=True), evidence=old) is None
    assert row(v=wait("customer"), evidence=old) is None


def test_engineering_cases_are_not_escalations():
    assert row(v=wait("engineering"), evidence=[ev("email", email(9, "us"))]) is None


def test_unread_links_are_reported_rather_than_assumed_quiet():
    got = row(evidence=[ev("email", email(9, "us")), ev("jira", ok=False)])
    assert got["checked"] == ["Email"] and got["not_read"] == ["Jira"]


def test_repeated_emails_count_once():
    mail = email(7, "us")
    assert row(evidence=[ev("email", mail), ev("email", mail)])["our_follow_ups"] == 1


def test_no_party_placeholder_shows_as_not_set():
    assert row(case(third_parties=["NA"]), evidence=[ev("email", email(9, "us"))])["waiting_on"] == ["Not set"]


def test_a_support_case_folds_into_its_dsp_case():
    dsp = case(id="d", case_number="D1", children=[{"id": "s"}])
    support = case(id="s", case_number="S1", record_type="Support", parent={"id": "d"})
    mail = [ev("email", email(9, "us"))]
    got = escalations.build([(support, wait(), mail), (dsp, wait(), mail)], 5, NOW, "DSP")
    assert got["total"] == 1 and got["cases"][0]["case_number"] == "D1"
    assert got["cases"][0]["support_cases"] == ["S1"] and "_related" not in got["cases"][0]


def test_a_support_case_stays_when_its_dsp_case_is_not_listed():
    support = case(id="s", record_type="Support", parent={"id": "d"})
    got = escalations.build([(support, wait(), [ev("email", email(9, "us"))])], 5, NOW, "DSP")
    assert got["total"] == 1


def test_longest_silence_first_and_summary_by_party():
    a = case(id="a", case_number="A")
    b = case(id="b", case_number="B", third_parties=["Other"])
    c = case(id="c", case_number="C")
    got = escalations.build([(a, wait(), [ev("email", email(6, "us"))]),
                             (b, wait(), [ev("email", email(20, "us"))]),
                             (c, wait(), [ev("email", email(9, "us"))])], 5, NOW)
    assert [r["case_number"] for r in got["cases"]] == ["B", "C", "A"]
    assert got["summary"][0] == {"name": "AcmeEats", "cases": 2, "longest_days": 9}


def test_a_reply_beyond_the_emails_with_text_still_counts():
    log = [{"who": "a@acme.com", "at": ago(10), "party": "third_party", "ours": False, "ack": False},
           {"who": "b@acme.com", "at": ago(2), "party": "third_party", "ours": False, "ack": True}]
    got = row(evidence=[ev("email", email(20, "us")), ev("email", *log)])
    assert got["days_silent"] == 10 and got["last_ack_at"] == ago(2)


def test_a_reply_on_the_linked_support_case_counts_for_the_dsp_case():
    dsp = case(id="d", case_number="D1", children=[{"id": "s"}])
    support = case(id="s", case_number="S1", record_type="Support", parent={"id": "d"})
    got = escalations.build([
        (dsp, wait(), [ev("email", email(20, "us"))]),
        (support, wait("us"), [ev("email", email(7, "third_party"), email(1, "customer"), email(1, "us"))]),
    ], 5, NOW, "DSP")
    assert got["cases"][0]["days_silent"] == 7 and got["cases"][0]["our_follow_ups"] == 0


def test_a_case_with_two_aggregators_counts_under_both():
    c = case(third_parties=["AcmeEats", "Other"])
    got = escalations.build([(c, wait(), [ev("email", email(9, "us"))])], 5, NOW)
    assert got["total"] == 1 and {s["name"] for s in got["summary"]} == {"AcmeEats", "Other"}


def test_a_business_id_stands_in_for_a_missing_biz_id():
    assert escalations.business({"business_name": "Biz One", "business_id": "222"}) == {"business": "Biz One", "biz_id": "222"}
