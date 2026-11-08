from datetime import datetime, timedelta, timezone

from interpreter.monitor import auto_checks as ac
from interpreter.monitor import case_details, config, today

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
QUESTIONS = {"order": {"card_id": "11", "name": "ORD-01 Order"},
             "order_status": {"card_id": "12", "name": "ORD-02 Status"},
             "order_items": {"card_id": "13", "name": "ITM-01 Items"},
             "item_stock": {"card_id": "14", "name": "ITM-02 Stock"},
             "menu_publish": {"card_id": "15", "name": "MNU-01 Publishes"},
             "store_actions": {"card_id": "16", "name": "STR-01 Store"}}
RULES = [{"name": "Disabled item", "sub_modules": ["Order for disabled item"],
          "checks": ["order", "order_items", "item_stock"]},
         {"name": "Orders", "modules": ["Orders"], "checks": ["order", "order_status", "order_items"]},
         {"name": "Store", "modules": ["Stores"], "checks": ["store_actions"]},
         {"name": "Menu", "modules": ["Menu"], "checks": ["menu_publish"]}]
CFG = {"sla_start": "2026-11-11T00:00:00+00:00", "dependency_record_type": "DSP", "auto_checks": RULES,
       "check_questions": QUESTIONS, "platform_aliases": {"Smiles": "eateasy"}}


def case(**kw):
    return {"id": "c1", "case_number": "42", "subject": "Problem", "description": ".", "owner": "Asha",
            "record_type": "DSP", "third_parties": ["Zomato"], "location_id": "133999, 134000",
            "biz_id": "777", "created_at": "2026-11-12T03:30:00+00:00", **kw}


def saved(kind, rows=3, at="2026-11-12T05:00:00+00:00", **kw):
    q = QUESTIONS[kind]
    return {"check_name": q["name"], "card_id": q["card_id"], "row_count": rows, "params": {},
            "summary": "s", "created_at": at, **kw}


def plan(c, checks=(), evidence=()):
    return ac.plan(c, list(evidence), list(checks), CFG)


def test_order_ids_come_from_the_case_then_its_emails():
    c = case(description="Order ID: 7788990011 was wrong")
    mail = {"kind": "email", "messages": [{"text": "order #5566778899 too, and Order ID: 7788990011"}]}
    assert case_details.order_ids(c, [mail]) == ["7788990011", "5566778899"]
    assert case_details.order_ids(case(description="order 12 was wrong"), []) == []


def test_a_store_case_gets_its_store_log_with_the_first_location_and_window():
    p = plan(case(module="Stores"))
    assert p["blocked"] == [] and len(p["due"]) == 1
    due = p["due"][0]
    assert due["name"] == "STR-01 Store" and due["card_id"] == "16"
    assert due["params"] == {"location_id": "133999", "platform": "zomato", "from": "2026-11-10 03:30:00"}


def test_the_platform_uses_the_catalogue_alias():
    assert plan(case(module="Menu", third_parties=["Smiles"]))["due"][0]["params"]["platform"] == "eateasy"


def test_an_order_case_without_an_order_id_waits():
    p = plan(case(module="Orders"))
    assert p["due"] == []
    assert [(b["kind"], b["reason"]) for b in p["blocked"]] == [
        ("order", "Needs order ID"), ("order_status", "Waits for ORD-01 Order"),
        ("order_items", "Waits for ORD-01 Order")]


def test_the_order_lookup_runs_first_then_its_followers_with_fill_hints():
    c = case(module="Orders", description="Order ID: 7788990011")
    first = plan(c)
    assert [d["kind"] for d in first["due"]] == ["order"]
    assert first["due"][0]["params"] == {"order_id": "7788990011", "case_date": "2026-11-12", "biz_id": "777",
                                         "location_id": "133999", "platform": "zomato"}
    after = plan(c, [saved("order")])
    assert [d["kind"] for d in after["due"]] == ["order_status", "order_items"]
    assert after["due"][0]["fill_from"]["check"] == "ORD-01 Order"
    assert "up_order_id" in after["due"][0]["fill_from"]["params"]
    assert [d["kind"] for d in after["done"]] == ["order"]


def test_an_order_not_found_blocks_what_follows_it():
    p = plan(case(module="Orders", description="Order ID: 7788990011"), [saved("order", rows=0)])
    assert p["due"] == [] and {b["reason"] for b in p["blocked"]} == {"ORD-01 Order found nothing"}


def test_the_stock_timeline_follows_the_items_and_nothing_runs_twice():
    c = case(sub_module="Order for disabled item", description="Order ID: 7788990011")
    p = plan(c, [saved("order"), saved("order_items")])
    assert [d["kind"] for d in p["due"]] == ["item_stock"]
    assert p["due"][0]["fill_from"]["check"] == "ITM-01 Items"
    assert plan(c, [saved("order"), saved("order_items"), saved("item_stock")])["due"] == []


def test_a_check_saved_by_name_alone_counts_as_done():
    p = plan(case(module="Stores"), [{"check_name": "STR-01 Store", "row_count": 2, "created_at": "x"}])
    assert p["due"] == [] and len(p["done"]) == 1


def test_a_case_with_no_rule_or_a_kind_with_no_question_gets_nothing():
    assert plan(case(module="Other"))["due"] == []
    cfg = {**CFG, "check_questions": {}}
    assert ac.plan(case(module="Stores"), [], [], cfg)["due"] == []


def test_only_new_aggregator_cases_are_in_scope():
    assert ac.in_scope(case(), CFG)
    assert not ac.in_scope(case(record_type="Support"), CFG)
    assert not ac.in_scope(case(created_at="2026-11-10T00:00:00+00:00"), CFG)


def test_due_lists_newest_case_first_with_blocked_apart():
    items = [(case(id="old", case_number="1", module="Stores", created_at="2026-11-11T05:00:00+00:00"), {}, []),
             (case(id="new", case_number="2", module="Stores"), {}, []),
             (case(id="ord", case_number="3", module="Orders"), {}, []),
             (case(id="sup", record_type="Support", module="Stores"), {}, [])]
    got = ac.due(items, {}, CFG, limit=1)
    assert [c["case_number"] for c in got["cases"]] == ["2"] and got["more"] == 1 and got["total"] == 2
    assert {b["case_number"] for b in got["blocked"]} == {"3"}


def test_due_rows_are_one_per_check():
    rows = ac.due_rows("t", [(case(module="Stores"), {}, [])], {}, CFG, NOW.isoformat())
    assert rows == [{"tenant_id": "t", "case_id": "c1", "kind": "store_actions", "case_number": "42",
                     "check_name": "STR-01 Store", "card_id": "16",
                     "params": {"location_id": "133999", "platform": "zomato", "from": "2026-11-10 03:30:00"},
                     "fill_from": None, "computed_at": NOW.isoformat()}]


def test_a_check_that_found_nothing_becomes_one_needs_me_card():
    c = case(module="Orders", description="Order ID: 7788990011")
    checks = {"c1": [saved("order", rows=0, params={"order_id": "7788990011"})],
              "c2": [saved("store_actions", rows=4)]}
    items = [(c, {}, []), (case(id="c2", module="Stores"), {}, [])]
    rows = ac.found_nothing(items, checks, CFG)
    assert [(r["case_id"], r["finding"]) for r in rows] == [("c1", "No order found with this ID")]
    card = ac.card(rows, today.CHECKS)
    assert card["kind"] == "checks" and card["case_ids"] == ["c1"] and card["title"] == "1 data check found nothing"
    assert "42 (Asha): No order found with this ID (order_id 7788990011)" in card["draft"]
    assert ac.card([], today.CHECKS) is None


def test_the_card_sits_with_the_agents_group_and_can_be_hidden():
    rows = ac.found_nothing([(case(module="Stores"), {}, [])], {"c1": [saved("store_actions", rows=0)]}, CFG)
    no_sla = {"partners": [], "total": 0, "breached": 0, "escalate": 0, "on_track": 0}
    view = today.build({"squads": []}, no_sla, {"share": 0}, {"total": 0, "owners": []}, 5, {}, {}, NOW,
                       checks=ac.card(rows, today.CHECKS))
    assert [(c["kind"], c["group"]) for c in view["cards"]] == [("checks", "agents")]
    done = {"checks:found-nothing": {"reviewed_at": (NOW - timedelta(hours=1)).isoformat()}}
    hidden = today.build({"squads": []}, no_sla, {"share": 0}, {"total": 0, "owners": []}, 5, done, {}, NOW,
                         checks=ac.card(rows, today.CHECKS))
    assert hidden["cards"] == [] and hidden["done"] == 1


def test_config_rejects_unknown_kinds_and_followers_without_their_first_check():
    assert config.validate({"auto_checks": RULES, "check_questions": QUESTIONS}) == []
    errors = config.validate({"auto_checks": [{"checks": ["order_status"]}, {"checks": ["oops"]}],
                              "check_questions": {"order": {}, "nope": {"card_id": "1"}}})
    assert "auto_checks: order_status has to come after order" in errors
    assert "auto_checks: unknown check kind 'oops'" in errors
    assert "check_questions.order needs a card_id or name" in errors
    assert any("unknown check kind 'nope'" in e for e in errors)


class _Table:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def __getattr__(self, op):
        def call(*a, **kw):
            self.log.append((self.name, op, a))
            return self
        return call

    def execute(self):
        return type("R", (), {"data": []})()


def test_record_checks_due_upserts_then_drops_older_rows(monkeypatch):
    from interpreter.monitor import service
    log = []
    sb = type("SB", (), {"table": lambda self, name: _Table(log, name)})()
    monkeypatch.setattr(service, "load_config", lambda sb, t: CFG)
    monkeypatch.setattr(service, "_cases_with_evidence", lambda sb, t, cfg: [(case(module="Stores"), {}, [])])
    assert service.record_checks_due(sb, "t", NOW) == 1
    ops = [(n, op) for n, op, _ in log if n == "monitor_checks_due"]
    assert ops == [("monitor_checks_due", "upsert"), ("monitor_checks_due", "delete"),
                   ("monitor_checks_due", "eq"), ("monitor_checks_due", "lt")]
    assert ("monitor_checks_due", "lt", ("computed_at", NOW.isoformat())) in log


def eng_case(**kw):
    return case(record_type="Support", waiting_party="engineering", **kw)


def test_the_dsp_scan_can_be_switched_off():
    assert ac.in_scope(case(), CFG)
    assert not ac.in_scope(case(), {**CFG, "auto_checks_dsp": False})


def test_engineering_cases_are_scanned_only_when_switched_on():
    assert not ac.in_scope(eng_case(), CFG)
    on = {**CFG, "auto_checks_engineering": True}
    assert ac.in_scope(eng_case(), on)
    github = case(record_type="Support", links=[{"kind": "github", "url": "u"}])
    assert ac.in_scope(github, on)
    assert not ac.in_scope(case(record_type="Support"), on)
    assert not ac.in_scope(eng_case(created_at="2026-11-10T00:00:00+00:00"), on)


def test_an_engineering_case_with_its_own_dsp_case_is_checked_there():
    on = {**CFG, "auto_checks_engineering": True}
    linked = eng_case(children=[{"id": "d1", "record_type": "DSP"}])
    assert not ac.in_scope(linked, on)
    assert ac.in_scope(linked, {**on, "auto_checks_dsp": False})


def test_store_menu_and_stock_checks_need_the_aggregator_name():
    p = plan(case(module="Stores", third_parties=[]))
    assert p["due"] == [] and p["blocked"][0]["reason"] == "Needs aggregator name"


def test_the_switches_must_be_true_or_false():
    assert config.validate({"auto_checks_dsp": "yes"}) == ["auto_checks_dsp must be true or false"]
    assert config.validate({"auto_checks_dsp": False, "auto_checks_engineering": True}) == []


def test_engineering_cases_join_the_agents_page_only_when_switched_on():
    eng, linked, dsp = eng_case(id="e"), eng_case(id="l", parent={"id": "d", "record_type": "DSP"}), case(id="d")
    items = [(eng, {}, []), (linked, {}, []), (dsp, {}, []), (case(id="s", record_type="Support"), {}, [])]
    assert ac.engineering_items(items, CFG) == []
    assert [c["id"] for c, _, _ in ac.engineering_items(items, {**CFG, "auto_checks_engineering": True})] == ["e"]


def test_found_nothing_rows_become_please_check_notes():
    rows = ac.found_nothing([(case(module="Stores"), {}, [])],
                            {"c1": [saved("store_actions", rows=0, params={"location_id": "133999"})]}, CFG)
    assert ac.notes(rows) == {"c1": ["STR-01 Store found nothing: No store on/off action in the window "
                                     "(location_id 133999)"]}
