from datetime import datetime, timedelta, timezone

from interpreter.monitor import log_expiry as le
from interpreter.monitor import today

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
CFG = {"log_retention_days": {"PartnerA": 7}, "log_topics": ["Orders", "Lost order"], "log_warn_days": 3}


def case(days=5.0, id="1", **kw):
    return {"id": id, "case_number": f"c{id}", "subject": "Order wrong", "owner": "Asha", "module": "Orders",
            "third_parties": ["PartnerA"], "created_at": (NOW - timedelta(days=days)).isoformat(), **kw}


def build(*cases):
    return le.build(list(cases), CFG, NOW)


def test_a_case_near_the_limit_is_warned():
    got = build(case(5))[0]
    assert (got["name"], got["rows"][0]["days_left"], got["gone"]) == ("PartnerA", 2, 0)
    assert "keeps logs for only 7 days" in got["draft"] and "c1 (Asha)" in got["draft"]


def test_a_fresh_case_has_no_warning_yet():
    assert build(case(1)) == []


def test_a_case_past_the_limit_only_counts_as_gone():
    assert build(case(9)) == []
    assert build(case(9), case(5, id="2"))[0]["gone"] == 1


def test_other_aggregators_and_topics_are_ignored():
    assert build(case(5, third_parties=["PartnerB"])) == []
    assert build(case(5, module="Menu")) == []


def test_a_sub_module_can_match_and_names_ignore_case():
    assert build(case(5, module="Menu", sub_module="Lost order", third_parties=["partnera"]))


def test_a_support_case_and_its_aggregator_case_warn_once():
    parent = case(5, id="1")
    child = case(4.9, id="2", parent={"id": "1"})
    assert len(build(parent, child)[0]["rows"]) == 1


def test_an_older_linked_case_does_not_write_off_saveable_logs():
    older = case(10, id="2", parent={"id": "1"})
    got = build(case(6, id="1"), older)[0]
    assert ([r["case_number"] for r in got["rows"]], got["gone"]) == (["c1"], 0)


def test_most_urgent_comes_first():
    rows = build(case(4.5, id="1"), case(6.5, id="2"))[0]["rows"]
    assert [r["case_number"] for r in rows] == ["c2", "c1"]


def test_the_card_is_red_on_the_last_day():
    card = today._logs_card(build(case(6.5), case(9, id="2"))[0])
    assert card["title"] == "Save logs for 1 PartnerA case"
    assert (card["chip"], card["tone"]) == ("12h left", "exception")
    assert "1 older may already be gone" in card["why"]
