"""
Seed `intake_checklists` (migration 101) with a starter set for a
restaurant-ops / UrbanPiper-style support desk, so the `clarify` node asks
targeted questions instead of free-writing vague ones.

    python -m scripts.seed_intake_checklists                       # default tenant
    python -m scripts.seed_intake_checklists --tenant <uuid>
    python -m scripts.seed_intake_checklists --list                # show what's seeded

Idempotent: for each (tenant_id, label) it deletes any existing row and
re-inserts, so editing a checklist here and re-running is the update path.
Each checklist owns its `match` (module / submodule / case_type exact,
`keywords` = ANY hit in subject+body+topic) and its ordered `signals`.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402

DEFAULT_TENANT = "ee4102db-ab47-4deb-b28b-9efc5a02575b"  # "gunner"


def _sig(key, label, question, *, required=True, detect=None, sf_field=None,
         ctx=None, vision=False):
    s = {"key": key, "label": label, "question": question, "required": required}
    if detect:
        s["detect"] = detect
    s["lands_in"] = {"sf_field": sf_field} if sf_field else {"ctx": ctx or key}
    if vision:
        s["vision"] = True
    return s


CHECKLISTS: list[dict] = [
    {
        "label": "Menu images not visible on delivery channels",
        "priority": 20,
        "match": {
            "keywords": ["image", "images", "photo", "photos", "picture", "pictures",
                         "not visible", "not showing", "missing image", "blank image"],
        },
        "signals": [
            _sig("channels", "Affected delivery channel(s)",
                 "Which delivery channels show the problem — Swiggy, Zomato, both, or others?",
                 detect={"any_of": ["swiggy", "zomato", "ubereats", "uber eats",
                                    "dunzo", "magicpin", "amazon food"]},
                 ctx="affected_channels"),
            _sig("outlet_ref", "Outlet / store identifier",
                 "What is the outlet ID (or the exact store name and city) where images are missing?",
                 detect={"regex": r"\b(outlet|store|location|biz|branch)\s*[:#]?\s*[A-Za-z0-9-]{3,}"},
                 ctx="outlet_ref"),
            _sig("scope", "All items or specific items",
                 "Is every item affected, or only specific items? If specific, which ones?",
                 detect={"any_of": ["all items", "every item", "entire menu", "whole menu",
                                    "few items", "some items", "one item", "specific item"]},
                 ctx="image_issue_scope"),
            _sig("last_publish", "Most recent publish status",
                 "In Publish Logs for that outlet, what is the status and time of the most recent menu publish?",
                 ctx="last_publish_status"),
            _sig("error_text", "Error report text",
                 "Does the Error Report show any code or message for the image upload? Paste it exactly.",
                 required=False,
                 detect={"regex": r"\b[45]\d\d\b|error|failed|failure|rejected"},
                 ctx="publish_error_text"),
            _sig("screenshot", "Screenshot from the channel app",
                 "Please attach a screenshot of one affected item as it appears in the "
                 "Swiggy / Zomato customer app.",
                 detect={"attachment_type": "image"}, ctx="has_screenshot", vision=True),
        ],
    },
    {
        "label": "Menu / price / stock out of sync with a channel",
        "priority": 18,
        "match": {
            "keywords": ["price", "prices", "pricing", "old price", "wrong price",
                         "not updated", "out of sync", "sync issue", "menu sync",
                         "stock", "out of stock", "in stock", "86", "item missing"],
        },
        "signals": [
            _sig("channels", "Affected delivery channel(s)",
                 "Which channels are out of sync — Swiggy, Zomato, both, or others?",
                 detect={"any_of": ["swiggy", "zomato", "ubereats", "uber eats",
                                    "dunzo", "magicpin"]},
                 ctx="affected_channels"),
            _sig("outlet_ref", "Outlet / store identifier",
                 "Which outlet ID (or store name + city) is showing the stale data?",
                 detect={"regex": r"\b(outlet|store|location|biz|branch)\s*[:#]?\s*[A-Za-z0-9-]{3,}"},
                 ctx="outlet_ref"),
            _sig("field", "What is stale",
                 "What exactly is wrong on the channel — item price, item availability, "
                 "add-ons, or the item itself? Give one concrete example (item name, "
                 "value you set vs. value shown).",
                 ctx="sync_discrepancy"),
            _sig("changed_at", "When the change was made in UrbanPiper",
                 "When did you make the change in UrbanPiper (date and rough time)?",
                 detect={"regex": r"\b(today|yesterday|\d{1,2}[:/ ]\d{1,2}|\d{1,2}\s*(am|pm))\b"},
                 ctx="change_made_at"),
            _sig("last_publish", "Most recent publish / sync status",
                 "What does the Publish Log show for that outlet after your change — success, "
                 "failed, or nothing?",
                 ctx="last_publish_status"),
        ],
    },
    {
        "label": "Menu publish / push to channel failed",
        "priority": 16,
        "match": {
            "keywords": ["publish failed", "publish error", "push failed", "could not publish",
                         "unable to publish", "publish stuck", "menu not pushing",
                         "503", "500", "timeout", "gateway"],
        },
        "signals": [
            _sig("channels", "Target channel(s) of the publish",
                 "Which channel were you publishing to — Swiggy, Zomato, both, or others?",
                 detect={"any_of": ["swiggy", "zomato", "ubereats", "uber eats", "dunzo", "magicpin"]},
                 ctx="affected_channels"),
            _sig("outlet_ref", "Outlet / store identifier",
                 "Which outlet ID (or store name + city) fails to publish?",
                 detect={"regex": r"\b(outlet|store|location|biz|branch)\s*[:#]?\s*[A-Za-z0-9-]{3,}"},
                 ctx="outlet_ref"),
            _sig("error_text", "Exact error text / code",
                 "Paste the exact error message or code shown in the Publish Log or Error Report.",
                 detect={"regex": r"\b[45]\d\d\b|error|failed|failure|exception|timeout"},
                 ctx="publish_error_text"),
            _sig("when", "When it started",
                 "When did publishing last work, and when did it start failing?",
                 ctx="failure_started_at"),
            _sig("screenshot", "Screenshot of the failure",
                 "Please attach a screenshot of the Publish Log / error screen.",
                 required=False,
                 detect={"attachment_type": "image"}, ctx="has_screenshot", vision=True),
        ],
    },
    {
        "label": "Orders not reaching the POS / missing orders",
        "priority": 14,
        "match": {
            "keywords": ["order not received", "missing order", "orders not coming",
                         "order not reflecting", "not receiving orders", "order missed",
                         "pos not getting", "no orders"],
        },
        "signals": [
            _sig("channels", "Channel the orders come from",
                 "Which channel's orders are missing — Swiggy, Zomato, both, or others?",
                 detect={"any_of": ["swiggy", "zomato", "ubereats", "uber eats", "dunzo", "magicpin"]},
                 ctx="affected_channels"),
            _sig("outlet_ref", "Outlet / store identifier",
                 "Which outlet ID (or store name + city) is not receiving orders?",
                 detect={"regex": r"\b(outlet|store|location|biz|branch)\s*[:#]?\s*[A-Za-z0-9-]{3,}"},
                 ctx="outlet_ref"),
            _sig("pos", "POS / aggregator setup",
                 "Which POS or system should the orders land in, and is it showing online in UrbanPiper?",
                 ctx="pos_system"),
            _sig("example_order", "A specific missing order",
                 "Give one example — the channel order ID and the time it was placed.",
                 detect={"regex": r"\border\s*(id|#|no)?\s*[:#]?\s*[A-Za-z0-9-]{4,}"},
                 ctx="example_order_id"),
            _sig("window", "Time window",
                 "Since when have orders been missing (date and time)?",
                 ctx="issue_window"),
        ],
    },
]


def seed(tenant_id: str) -> int:
    sb = get_supabase()
    n = 0
    for c in CHECKLISTS:
        sb.table("intake_checklists").delete().eq("tenant_id", tenant_id).eq(
            "label", c["label"]
        ).execute()
        sb.table("intake_checklists").insert(
            {
                "tenant_id": tenant_id,
                "label": c["label"],
                "match": c["match"],
                "signals": c["signals"],
                "priority": c["priority"],
                "enabled": True,
            }
        ).execute()
        n += 1
        print(f"  seeded  {c['label']}  ({len(c['signals'])} signals)")
    return n


def show(tenant_id: str) -> None:
    sb = get_supabase()
    rows = (
        sb.table("intake_checklists")
        .select("label, priority, match, signals, enabled")
        .eq("tenant_id", tenant_id)
        .order("priority", desc=True)
        .execute()
        .data
        or []
    )
    if not rows:
        print("  (none)")
        return
    for r in rows:
        req = sum(1 for s in r["signals"] if s.get("required", True))
        print(f"  [{r['priority']:>3}] {r['label']}  — {req} required / "
              f"{len(r['signals'])} signals  {'' if r['enabled'] else '(disabled)'}")
        print(f"        match: {r['match']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=DEFAULT_TENANT, help="tenant_id (uuid)")
    ap.add_argument("--list", action="store_true", help="show seeded checklists and exit")
    args = ap.parse_args()

    if args.list:
        show(args.tenant)
        return
    n = seed(args.tenant)
    print(f"done: {n} checklist(s) for tenant {args.tenant}")


if __name__ == "__main__":
    main()
