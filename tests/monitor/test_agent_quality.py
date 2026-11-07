from datetime import datetime, timedelta, timezone

from interpreter.monitor import agent_quality as aq

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
CFG = {"required_details": [{"modules": ["Orders"], "needs": ["order"]}], "platform_aliases": {"PartnerB": "otherplat"}}
FACTS = {"100": {"partnera": {"platform_id": "A1", "biz_id": "9"}, "otherplat": {"platform_id": "B1", "biz_id": "9"}},
         "200": {"partnera": {"platform_id": "A2", "biz_id": "9"}},
         "300": {"partnera": {"platform_id": None, "biz_id": "9"}}}
NO_COACH = {"agents": []}


def case(**kw):
    return {"id": "1", "case_number": "42", "subject": "Problem", "description": ".", "owner": "Asha",
            "business_name": "Biz One", "biz_id": "9", "location_id": "100", "platform_id": "A1",
            "third_parties": ["PartnerA"], "created_at": (NOW - timedelta(days=2)).isoformat(), **kw}


def mail(text, ours=True, at="2026-11-10T10:00:00Z"):
    return {"kind": "email", "messages": [{"text": text, "ours": ours, "at": at}]}


def build(*items, coaching=NO_COACH):
    return aq.build([(c, {}, e) for c, e in items], FACTS, coaching, CFG, NOW)


def test_matching_ids_pass():
    assert aq.id_problems(case(), FACTS, {}) == ([], [])


def test_a_platform_id_from_another_location_is_wrong():
    wrong, _ = aq.id_problems(case(platform_id="A2"), FACTS, {})
    assert wrong == ["Location 100 has PartnerA ID A1, but the case says A2"]


def test_a_location_from_another_business_is_wrong():
    wrong, _ = aq.id_problems(case(biz_id="8"), FACTS, {})
    assert wrong == ["Location 100 belongs to business 9, not 8"]


def test_several_locations_are_paired_in_order():
    ok = case(location_id="100, 200", platform_id="A1,A2")
    assert aq.id_problems(ok, FACTS, {}) == ([], [])
    swapped = case(location_id="100,200", platform_id="A2, A1")
    assert len(aq.id_problems(swapped, FACTS, {})[0]) == 2


def test_a_platform_named_differently_uses_the_mapping():
    c = case(third_parties=["PartnerB"], platform_id="B1")
    assert aq.id_problems(c, FACTS, {"PartnerB": "otherplat"}) == ([], [])


def test_a_gap_in_our_own_records_is_a_check_not_a_mistake():
    wrong, check = aq.id_problems(case(location_id="300", platform_id="A9"), FACTS, {})
    assert wrong == [] and "can't be confirmed" in check[0]


def test_a_location_not_copied_yet_is_skipped():
    assert aq.id_problems(case(location_id="999", platform_id="A9"), FACTS, {}) == ([], [])


def test_another_stores_payload_is_wrong():
    got = aq.payload_problem(case(), [mail("rqst_payload: https://s3/1111111-777-PARTNERA-2026-10-08_124937-abc")])
    assert got == "Payload is for location 777, but the case is 100"


def test_the_new_payload_link_format_is_read_too():
    got = aq.payload_problem(case(), [mail("https://s3/publish-menu/x/versions/1/locations/777/platforms/zo/abc")])
    assert "location 777" in got


def test_a_second_store_for_comparison_is_fine():
    text = "https://s3/76446713-100-ZO-2026-10-12_1 and https://s3/76446713-777-ZO-2026-09-23_1"
    assert aq.payload_problem(case(), [mail(text)]) is None


def test_a_payload_quoted_by_the_aggregator_does_not_count():
    assert aq.payload_problem(case(), [mail("https://s3/1111111-777-PARTNERA-2026-10-08_1", ours=False)]) is None


def test_no_payload_is_no_problem():
    assert aq.payload_problem(case(), [mail("no link here")]) is None


def test_the_raiser_is_who_signed_the_first_email():
    first = mail("Hi team\nRegards,\nMeera Nair| Specialist", at="2026-11-10T10:00:00Z")
    later = mail("Any update?\nRegards,\nNeha Y | Specialist", at="2026-11-11T10:00:00Z")
    assert aq.raiser(case(), [later, first]) == "Meera Nair"


def test_someone_who_only_chased_is_not_the_raiser():
    first = mail("Issue details with no sign-off", at="2026-11-10T10:00:00Z")
    later = mail("Any update?\nRegards,\nNeha Y | Specialist", at="2026-11-11T10:00:00Z")
    assert aq.raiser(case(), [first, later]) == "Asha"


def test_a_short_signature_uses_the_owners_full_name():
    assert aq.raiser(case(owner="Ravi K"), [mail("Regards,\nRavi | Specialist")]) == "Ravi K"


def test_names_from_different_systems_match():
    for a, b in (("asha.k", "Asha Kumar"), ("ravidkumar", "Ravi D Kumar"), ("Meera", "Meera Nair"),
                 ("asha.k", "Asha")):
        assert aq.same_person(a, b), (a, b)
    assert not aq.same_person("anil.s", "Arjun Singh")
    assert not aq.same_person("neha.m", "Neha Kapoor")


def test_the_raiser_falls_back_to_the_owner():
    assert aq.raiser(case(), [mail("no signature")]) == "Asha"


def test_wrong_raises_are_counted_per_agent_with_the_main_mistake():
    bad = case(module="Orders")
    good = case(id="2", case_number="43")
    got = build((bad, []), (good, []))
    a = got["agents"][0]
    assert (a["agent"], a["raised"], a["wrong"], a["main_mistake"]) == ("Asha", 2, 1, "Missing details")
    assert "42: Missing order ID" in a["message"]
    assert (got["raised"], got["wrong"], got["share"]) == (2, 1, 50)


def test_a_please_check_case_is_not_counted_as_wrong():
    got = build((case(location_id="300", platform_id="A9"), []))
    a = got["agents"][0]
    assert a["wrong"] == 0 and len(a["check"]) == 1 and a["message"] is None


def test_a_missing_item_name_is_a_check_because_matching_is_unreliable():
    cfg = {**CFG, "required_details": [{"modules": ["Menu"], "needs": ["item"]}]}
    found = aq.check_case(case(module="Menu"), [], FACTS, cfg)
    assert found["problems"] == [] and found["check"] == ["No item name found in the case or emails"]


def test_old_cases_are_outside_the_window():
    assert build((case(created_at=(NOW - timedelta(days=31)).isoformat()), []))["raised"] == 0


def test_github_wrong_raises_join_the_same_agent_by_name():
    coach = {"agents": [{"agent": "asha", "raised": 4, "wrong": 2, "message": "",
                         "issues": [{"title": "Issue", "url": "u", "reasons": ["config-related"]}] * 2}]}
    a = build((case(), []), coaching=coach)["agents"][0]
    assert (a["raised"], a["wrong"], a["main_mistake"]) == (5, 2, "Didn't need engineering")
    assert "didn't need an engineer" in a["message"]


def test_a_github_name_joins_the_salesforce_name():
    coach = {"agents": [{"agent": "asha.k", "raised": 2, "wrong": 2, "message": "",
                         "issues": [{"title": "Issue", "url": "u", "reasons": ["x"]}] * 2}]}
    got = build((case(owner="Asha Kumar"), []), coaching=coach)
    assert [a["agent"] for a in got["agents"]] == ["Asha Kumar"]


def test_worst_first():
    got = build((case(owner="Good"), []), (case(id="2", owner="Bad", module="Orders"), []))
    assert [a["agent"] for a in got["agents"]] == ["Bad", "Good"]
