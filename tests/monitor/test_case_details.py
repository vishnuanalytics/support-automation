from datetime import datetime, timedelta, timezone

from interpreter.monitor import case_details as cd

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
RULES = [{"sub_modules": ["No orders"], "needs": ["location"]},
         {"sub_modules": ["Item hidden"], "needs": ["item", "location", "platform"]},
         {"modules": ["Stores"], "needs": ["location"]},
         {"modules": ["Orders"], "needs": ["order"]}]
CFG = {"sla_start": "2026-11-11T00:00:00+00:00", "required_details": RULES}


def case(**kw):
    return {"id": "1", "case_number": "42", "subject": "Problem", "description": ".", "owner": "Asha",
            "business_name": "Biz One", "created_at": (NOW - timedelta(hours=2)).isoformat(), **kw}


def mail(text):
    return {"kind": "email", "messages": [{"text": text}]}


def test_every_case_needs_a_business():
    assert cd.missing(case(business_name=None, module="Other"), RULES) == ["business name"]


def test_an_order_case_needs_an_order_id():
    assert cd.missing(case(module="Orders"), RULES) == ["order ID"]
    for text in ("Order ID: 1234567890", "PartnerA Order #2345678901", "Order ID - 3456789012",
                 "PartnerB Order ABCD1234567890", "for Order 4567890123"):
        assert cd.missing(case(module="Orders", description=text), RULES) == [], text


def test_the_order_id_can_be_in_the_email():
    assert cd.missing(case(module="Orders"), RULES, [mail("Order ID: 1234567890")]) == []


def test_a_short_number_or_the_word_order_is_not_an_id():
    assert cd.missing(case(module="Orders", description="order 12 was wrong"), RULES) == ["order ID"]


def test_no_orders_received_needs_a_location_not_an_order():
    assert cd.missing(case(module="Orders", sub_module="No orders", location_id="9"), RULES) == []


def test_a_store_case_needs_a_location_or_platform_id():
    assert cd.missing(case(module="Stores"), RULES) == ["location or platform ID"]
    assert cd.missing(case(module="Stores", location_id="9"), RULES) == []
    assert cd.missing(case(module="Stores", platform_id="9"), RULES) == []


def item_case(**kw):
    return case(sub_module="Item hidden", location_id="9", third_parties=["PartnerA"], **kw)


def test_an_item_case_needs_all_three():
    got = cd.missing(case(sub_module="Item hidden"), RULES)
    assert got == ["item name", "location or platform ID", "platform name"]


def test_an_item_name_can_be_labelled_quoted_or_named():
    assert cd.missing(item_case(), RULES) == ["item name"]
    assert cd.missing(item_case(), RULES, [mail("Item name : Avocado Toast")]) == []
    assert cd.missing(item_case(description='Missing "Honey Cake" here'), RULES) == []
    assert cd.missing(item_case(subject="Not showing for Russian Honey"), RULES) == []


def test_quotes_in_an_email_are_not_an_item_name():
    assert cd.missing(item_case(), RULES, [mail('We said "please check" for Urban Piper')]) == ["item name"]


def test_only_cases_raised_after_the_start_are_flagged():
    old = case(module="Orders", created_at=(NOW - timedelta(days=5)).isoformat())
    assert cd.build([(old, {}, [])], CFG, NOW) is None
    assert cd.build([(case(module="Orders"), {}, [])], {"required_details": RULES}, NOW) is None


def test_a_complete_case_needs_no_card():
    assert cd.build([(case(module="Stores", location_id="9"), {}, [])], CFG, NOW) is None
