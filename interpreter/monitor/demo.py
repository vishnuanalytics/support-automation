"""Eight fake cases that exercise every verdict, for demos and first-run."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import config
from .links import parse

CONFIG = config.merge({
    "third_party_label": "Aggregator", "customer_label": "Merchant",
    "focus_partners": ["FoodRunner", "QuickEats"],
    "fields": {"waiting_on": "Waiting_Party__c", "link": "External_Link__c",
               "last_reply_from": "Last_Reply_From__c", "target_at": "Target_Date__c",
               "third_party": "Partner__c", "bu": "Business_Unit__c", "brand": "Brand__c",
               "biz_id": "Location_ID__c", "product": "Product__c", "module": "Module__c",
               "sub_module": "Sub_Module__c"},
    "third_party_overrides": {"FoodRunner": 12},
})

GH_A = "https://github.com/demo-org/incidents/issues/101"
GH_B = "https://github.com/demo-org/incidents/issues/102"
GH_C = "https://github.com/demo-org/incidents/issues/103"
SLACK_A = "https://demo.slack.com/archives/C0DEMO1/p1700000000000100"
SLACK_B = "https://demo.slack.com/archives/C0DEMO2/p1700000000000200"


def _ago(now: datetime, hours: float) -> str:
    return (now - timedelta(hours=hours)).isoformat()


def build(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    t = lambda h: _ago(now, h)  # noqa: E731

    def case(num: int, subject: str, wait: str | None, link: str | None, **kw) -> dict:
        party = {"Engineer": "engineering", "Aggregator": "third_party"}.get(wait or "")
        return {"id": f"demo-{num}", "source": "demo", "case_number": f"0000{num + 4100}",
                "subject": subject, "status": "In Progress", "priority": "Medium",
                "record_type": "Support", "owner": "Demo Lead", "created_at": t(120),
                "last_modified_at": t(30), "url": None, "waiting_raw": wait,
                "waiting_party": party, "links": parse(link), "third_parties": [],
                "brand": "Tasty Bites", "biz_id": "BZ-1042", "product": "Hub", "module": "Orders",
                "sub_module": "Relay", "children": [], "parent": None,
                "bu": "North", "brand_stats": {"open": 4, "last_30_days": 9}, **kw}

    def gh(url: str, msgs: list, state: str = "open") -> dict:
        n = url.rsplit("/", 1)[1]
        return {"kind": "github", "url": url, "label": f"demo-org/incidents#{n}", "ok": True,
                "state": state, "title": "Demo incident", "labels": ["incident"],
                "messages": msgs}

    def msg(who: str, ours: bool, hours: float, text: str, url: str) -> dict:
        return {"who": who, "ours": ours, "at": t(hours), "text": text, "url": url}

    gh_a = gh(GH_A, [msg("support-lead", True, 40, "Orders not relaying for one outlet.", GH_A),
                     msg("backend-dev", False, 3, "Fix is on staging. Can you retest?", GH_A)])
    gh_b = gh(GH_B, [msg("backend-dev", False, 20, "Deployed the menu sync fix.", GH_B)], "closed")
    gh_c = gh(GH_C, [msg("support-lead", True, 12, "Three merchants report failed payouts.", GH_C)])
    slack_a = {"kind": "slack", "url": SLACK_A, "label": "Slack thread", "ok": True, "messages": [
        msg("Demo Lead", True, 30, "Please check rider assignment.", SLACK_A),
        msg("FoodRunner Ops", False, 2, "Need the order IDs to investigate.", SLACK_A)]}
    slack_b = {"kind": "slack", "url": SLACK_B, "label": "Slack thread", "ok": True, "messages": [
        msg("Demo Lead", True, 50, "Any update on the commission mismatch?", SLACK_B)]}

    cases = [
        case(1, "Orders not relaying to POS", "Engineer", GH_A, priority="High",
             last_modified_at=t(6)),
        case(2, "Menu sync failing after update", "Engineer", GH_B,
             target_at=t(30), module="Menu", sub_module="Sync"),
        case(3, "Rider not assigned on delivery orders", "Aggregator", SLACK_A,
             third_parties=["FoodRunner"], last_modified_at=t(10), module="Delivery"),
        case(4, "Commission mismatch on settlement", "Aggregator", None,
             third_parties=["QuickEats"], module="Finance", sub_module="Settlement", bu="South",
             record_type="DSP"),
        case(5, "Store timings not updating", "Engineer", GH_C, last_reply_from="customer",
             last_modified_at=t(2), module="Stores"),
        case(6, "Payout failed for outlet A", "Engineer", GH_C, last_modified_at=t(11),
             module="Finance", sub_module="Payout", brand="Spice Route"),
        case(7, "Payout failed for outlet B", "Engineer", GH_C, last_modified_at=t(11),
             module="Finance", sub_module="Payout", brand="Spice Route", priority="Urgent"),
        case(8, "Stale commission thread", "Aggregator", SLACK_B, last_modified_at=t(60),
             third_parties=["QuickEats"], module="Finance", sub_module="Settlement"),
        case(9, "Coupon stopped applying at checkout", "Engineer",
             "https://github.com/demo-org/incidents/issues/104", last_modified_at=t(1),
             module="Offers", priority="Low"),
    ]
    on_track = gh("https://github.com/demo-org/incidents/issues/104",
                  [msg("support-lead", True, 1, "Reproduced. Steps attached.",
                       "https://github.com/demo-org/incidents/issues/104")])
    evidence = {e["url"]: e for e in (gh_a, gh_b, gh_c, slack_a, slack_b, on_track)}

    timeline = {
        "emails": [{"from": "owner@example.com", "to": "support@example.com",
                    "subject": "Issue report", "text": "We need help with this today.",
                    "at": t(118), "incoming": True}],
        "comments": [{"text": "Raised with the owning team.", "at": t(100), "by": "Demo Lead"}],
        "history": [{"field": "Status", "old": "New", "new": "In Progress", "at": t(110),
                     "by": "Demo Lead"}],
    }

    resolved = [
        {"id": "demo-r1", "case_number": "00003901", "subject": "Orders stuck in relay",
         "module": "Orders", "sub_module": "Relay", "third_parties": [],
         "opened_at": t(900), "closed_at": t(700),
         "resolution_note": "Restarted the relay worker and replayed failed orders."},
        {"id": "demo-r2", "case_number": "00003822", "subject": "POS relay timeouts",
         "module": "Orders", "sub_module": "Relay", "third_parties": [],
         "opened_at": t(1500), "closed_at": t(1400),
         "resolution_note": "Merchant's POS token had expired; regenerated it."},
        {"id": "demo-r3", "case_number": "00003790", "subject": "Settlement short by 2%",
         "module": "Finance", "sub_module": "Settlement", "third_parties": ["QuickEats"],
         "opened_at": t(2000), "closed_at": t(1700),
         "resolution_note": "Aggregator corrected the commission slab."},
        {"id": "demo-r4", "case_number": "00003755", "subject": "Payout held for KYC",
         "module": "Finance", "sub_module": "Payout", "third_parties": [],
         "opened_at": t(2400), "closed_at": t(2300),
         "resolution_note": "Finance released the hold after KYC review."},
        {"id": "demo-r5", "case_number": "00003700", "subject": "Menu items missing after sync",
         "module": "Menu", "sub_module": "Sync", "third_parties": [],
         "opened_at": t(2600), "closed_at": t(2500),
         "resolution_note": "Re-published the menu from Hub."},
    ]
    return {"cases": cases, "evidence": evidence, "timeline": timeline, "resolved": resolved}
