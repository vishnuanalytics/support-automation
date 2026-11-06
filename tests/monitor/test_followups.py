from interpreter.monitor import followups


def item(**kw):
    base = {"id": "1", "case_number": "42", "subject": "S", "next_action": "Do it", "owner": "Asha Rao",
            "stage": "act", "hidden": False, "third_parties": [], "overdue": False, "score": 10,
            "mine": False}
    return {**base, **kw}


def test_only_cases_needing_something_today_are_listed():
    assert followups.action_for(item()) == "reply"
    assert followups.action_for(item(stage="raise")) == "raise"
    assert followups.action_for(item(stage="third_party", stale=True)) == "chase"
    assert followups.action_for(item(stage="third_party")) is None
    assert followups.action_for(item(stage="check", stale=True)) is None
    assert followups.action_for(item(hidden=True)) is None


def test_groups_by_owner_with_counts_and_a_message():
    got = followups.build([item(third_parties=["AcmeEats"]), item(id="2", case_number="43", stage="raise")])
    owner = got["owners"][0]
    assert owner["owner"] == "Asha Rao" and owner["counts"] == {"reply": 1, "raise": 1, "chase": 0}
    assert owner["message"].startswith("Hi Asha,") and "- 42 (AcmeEats): Do it" in owner["message"]


def test_replies_come_before_chases_and_higher_scores_first():
    got = followups.build([item(id="a", stage="third_party", stale=True, score=99),
                           item(id="b", score=5), item(id="c", score=50)])
    assert [c["id"] for c in got["owners"][0]["cases"]] == ["c", "b", "a"]


def test_the_lead_comes_first_then_the_busiest_agent():
    got = followups.build([item(owner="Busy"), item(id="2", owner="Busy"), item(id="3", owner="Lead", mine=True)])
    assert [o["owner"] for o in got["owners"]] == ["Lead", "Busy"]


def test_a_case_with_no_owner_is_still_listed():
    assert followups.build([item(owner=None)])["owners"][0]["owner"] == "Unassigned"
