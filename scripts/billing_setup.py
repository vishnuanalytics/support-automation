"""
Billing go-live step: creates the Stripe Price + Razorpay Plan objects the
self-serve tiers (Basic/Pro/Advanced) need before they're actually
checkout_available, then writes the resulting IDs onto
plans.stripe_price_id / plans.razorpay_plan_id. Free (trial) and Enterprise
(sales-assisted) deliberately have no price object and are left alone.

The exact same script for test mode now and going live later -- it doesn't
know or care which mode STRIPE_SECRET_KEY / RAZORPAY_KEY_ID are in, it just
uses whatever .env has. Prints which mode it detected (from the key
prefix) so a run is never ambiguous about whether it just created real,
billable objects.

    python -m scripts.billing_setup               # do it
    python -m scripts.billing_setup --dry-run      # show what would change
    python -m scripts.billing_setup --only stripe  # just one provider

Idempotent: a plan that already has a price id for a provider is skipped
for that provider. Re-running after an actual price change needs the old
id cleared by hand first -- Stripe Prices and Razorpay Plans are both
immutable once created (a real price change always means a new object in
either dashboard too, not an edit).
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv

load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402
from interpreter import payments  # noqa: E402

# Enterprise is sales-assisted (no checkout at all, deliberately no price
# object); Free isn't subscribed into via checkout either.
_SELF_SERVE_SLUGS = ("basic", "pro", "advanced")


def _mode(key: "str | None", live_prefix: str, test_prefix: str) -> str:
    if not key:
        return "NOT CONFIGURED"
    if key.startswith(live_prefix):
        return "LIVE"
    if key.startswith(test_prefix):
        return "test"
    return "unknown prefix"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", choices=["stripe", "razorpay"], default=None)
    args = ap.parse_args()

    stripe_key = os.environ.get("STRIPE_SECRET_KEY")
    razorpay_key = os.environ.get("RAZORPAY_KEY_ID")
    stripe_mode = _mode(stripe_key, "sk_live_", "sk_test_")
    razorpay_mode = _mode(razorpay_key, "rzp_live_", "rzp_test_")
    print(f"Stripe:   {stripe_mode}" + (f"  ({stripe_key[:12]}...)" if stripe_key else ""))
    print(f"Razorpay: {razorpay_mode}" + (f"  ({razorpay_key[:12]}...)" if razorpay_key else ""))
    if "LIVE" in (stripe_mode, razorpay_mode):
        print("\n*** LIVE mode detected -- this creates REAL, billable objects. ***")
    print()

    sb = get_supabase()
    rows = (sb.table("plans").select("*").in_("slug", list(_SELF_SERVE_SLUGS)).execute().data or [])
    missing = set(_SELF_SERVE_SLUGS) - {r["slug"] for r in rows}
    if missing:
        print(f"warning: plans table is missing {missing} -- run migration 112 first\n")

    for row in sorted(rows, key=lambda r: r["base_price_usd"] or 0):
        slug, name = row["slug"], row["name"]
        updates: dict[str, str] = {}

        if args.only in (None, "stripe"):
            if row.get("stripe_price_id"):
                print(f"{slug}: Stripe already set ({row['stripe_price_id']}) -- skipped")
            elif stripe_mode == "NOT CONFIGURED":
                print(f"{slug}: skipping Stripe (STRIPE_SECRET_KEY not set)")
            elif args.dry_run:
                print(f"{slug}: would create a Stripe Price for ${row['base_price_usd'] / 100:.2f}/mo")
            else:
                price_id = payments.stripe_create_price(
                    f"{name} — Support Automation", row["base_price_usd"])
                updates["stripe_price_id"] = price_id
                print(f"{slug}: created Stripe Price {price_id}")

        if args.only in (None, "razorpay"):
            if row.get("razorpay_plan_id"):
                print(f"{slug}: Razorpay already set ({row['razorpay_plan_id']}) -- skipped")
            elif razorpay_mode == "NOT CONFIGURED":
                print(f"{slug}: skipping Razorpay (RAZORPAY_KEY_ID not set)")
            elif args.dry_run:
                print(f"{slug}: would create a Razorpay Plan for INR {row['base_price_inr'] / 100:.2f}/mo")
            else:
                plan_id = payments.razorpay_create_plan(name, row["base_price_inr"])
                updates["razorpay_plan_id"] = plan_id
                print(f"{slug}: created Razorpay Plan {plan_id}")

        if updates and not args.dry_run:
            sb.table("plans").update(updates).eq("plan_id", row["plan_id"]).execute()

    print("\nDone." + (" (dry run -- nothing written)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
