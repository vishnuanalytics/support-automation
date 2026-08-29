"""
Create a few Accounts + Contacts + Cases in the connected Salesforce org so
`interpreter.run --sf-case <id>` has something realistic to run against.

Needs SF creds in .env (see SALESFORCE_SETUP.md). Prints the new Case Ids.
Safe to re-run — it always creates fresh records (no upsert); delete old
ones in Salesforce if they pile up.

    python scripts/sf_seed_cases.py
"""

from __future__ import annotations

import pathlib
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from interpreter.salesforce import _bad_field, _client, available  # noqa: E402

SEED = [
    {
        "tier": "basic", "region": "AMER",
        "account": "Indie Dev Co", "contact": ("Sam", "Rivera", "sam@indie.example"),
        "subject": "How do I create a webhook trigger in a Zap?",
        "body": "Free plan. I want my Zap to run when my app POSTs to a URL. "
                "How do I get the webhook URL and test it?",
    },
    {
        "tier": "premium", "region": "EMEA",
        "account": "Northwind Ltd", "contact": ("Priya", "Nair", "priya@northwind.example"),
        "subject": "Prorated charge looks wrong after mid-cycle plan upgrade",
        "body": "Moved Team -> Professional on the 12th; invoice shows a full-period "
                "charge, not prorated. Please confirm how proration is calculated.",
    },
    {
        "tier": "enterprise", "region": "AMER",
        "account": "Globex Enterprise", "contact": ("Alex", "Chen", "alex@globex.example"),
        "subject": "Production Zaps failing intermittently with 500s since 08:00 UTC",
        "body": "Large share of production Zaps erroring with 500s on the action step. "
                "Impacting order processing. Need someone urgently.",
    },
]


def _create(obj, payload: dict) -> str:
    """Create a record, retrying without any field the org rejects."""
    sf = _client()
    body = dict(payload)
    for _ in range(len(payload) + 1):
        try:
            return getattr(sf, obj).create(body)["id"]
        except Exception as e:  # noqa: BLE001
            bad = _bad_field(e)
            if bad and bad in body:
                print(f"  ({obj}: dropping unknown field {bad!r})")
                body.pop(bad)
                continue
            raise
    raise RuntimeError(f"could not create {obj}")


def main() -> int:
    if not available():
        sys.exit("no SF creds in .env — see SALESFORCE_SETUP.md")

    case_ids = []
    for row in SEED:
        acc_id = _create("Account", {
            "Name": row["account"],
            "Tier__c": row["tier"],           # dropped automatically if absent
            "BillingCountry": row["region"],
        })
        first, last, email = row["contact"]
        con_id = _create("Contact", {
            "FirstName": first, "LastName": last, "Email": email, "AccountId": acc_id,
        })
        case_id = _create("Case", {
            "Subject": row["subject"],
            "Description": row["body"],
            "Origin": "Web",
            "Status": "New",
            "AccountId": acc_id,
            "ContactId": con_id,
        })
        case_ids.append((row["tier"], case_id))
        print(f"{row['tier']:<11} Account={acc_id}  Contact={con_id}  Case={case_id}")

    print("\nrun one:")
    for tier, cid in case_ids:
        print(f"  python -m interpreter.run --flow 11111111-1111-1111-1111-111111111111 --sf-case {cid}   # {tier}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
