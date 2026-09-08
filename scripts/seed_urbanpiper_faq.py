"""
Curated Q->A knowledge for the gunner (UrbanPiper) demo — tight, on-topic
entries for the scenarios the flow is demoed on. Retrieval ranks a short
question-shaped entry far above crawled marketing prose, so these carry the
"can the bot actually answer it" load.

    python -m scripts.seed_urbanpiper_faq            # upsert all
    python -m scripts.seed_urbanpiper_faq --list

Idempotent per (source, title). Lands in its own `urbanpiper-faq` source so
it's easy to see / weight separately from the crawl.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402
from ingestion.sources.kb_common import embed_entry  # noqa: E402

TENANT = "ee4102db-ab47-4deb-b28b-9efc5a02575b"  # gunner
SOURCE_NAME = "urbanpiper-faq"
SECTION = "UrbanPiper FAQ"

# Each: (question-shaped title, markdown answer). Grounded in UrbanPiper's
# model — Business Manager, the menu publish path, channel (Swiggy/Zomato)
# integrations, store toggle, POS order webhooks.
ENTRIES: list[tuple[str, str]] = [
    (
        "Menu images are not showing on Swiggy / Zomato after publishing",
        """\
# Menu images not visible on the aggregator

Images live on the item in **Business Manager** and are pushed to the channel
on **menu publish**. When they don't appear on Swiggy/Zomato:

1. **Confirm the publish actually completed.** Business Manager -> the
   outlet -> *Publish Logs*. A publish that shows *Partial* or *Failed*
   means the image step didn't land.
2. **Check the image itself.** The aggregators reject images below their
   minimum resolution or over the size cap, and non-JP/PNG files. Re-upload a
   clean JPG/PNG that meets the channel's spec and publish again.
3. **Aggregator lag.** Swiggy/Zomato can take up to a few hours to render a
   newly pushed image even after a clean publish. If Publish Logs show
   success and the spec is fine, wait one refresh cycle before escalating.
4. **Item mapping.** If the item was only recently linked to the channel,
   the image pushes on the *next* full publish, not an incremental one — run
   a full menu publish for that outlet.

**To investigate a ticket we need:** the outlet ID (or store name + city),
which channel(s), whether it's all items or specific ones, the most recent
Publish Log status + time, and any error text from the Error Report.
""",
    ),
    (
        "Item prices are not updating on the channel / Swiggy shows the old price",
        """\
# Stale price on the aggregator

Prices sync to the channel on **menu publish**. A stale price on
Swiggy/Zomato almost always means the change never reached the channel:

1. **Was the price actually saved in Business Manager?** Open the item and
   confirm the new price is stored (not just typed into an unsaved field).
2. **Was a publish run after the change?** Price edits are not live — they
   ride the next publish. Business Manager -> outlet -> *Publish* -> confirm
   *Publish Logs* shows success **after** the edit timestamp.
3. **Channel-specific price override.** If the outlet has a per-channel price
   list, editing the base price won't move the channel price — update the
   channel price list for that outlet.
4. **Publish failed silently.** A *Failed* / *Partial* publish leaves the old
   price in place. Re-publish and watch the log.

**To investigate:** outlet ID, channel(s), an example item (name, price you
set vs. price shown), when the edit was made, and the Publish Log result
after that time.
""",
    ),
    (
        "An item is stuck out of stock / marking it in-stock doesn't work",
        """\
# Item availability won't change on the channel

Stock toggles go to the channel through the **item stock / mark-out-of-stock**
action, separate from a full menu publish.

- Toggling stock in Business Manager or via the Prime app sends an
  availability update per item per outlet. If it doesn't take:
  1. Check the outlet is **online** for that channel — a store that is
     toggled off can't receive item updates.
  2. Check the item is **mapped** to that channel for that outlet. An
     unmapped item can't be toggled there.
  3. Retry the toggle; a single failed call does not auto-retry.
- If an item keeps flipping back to out-of-stock, a POS/stock integration is
  pushing the state — check the POS's stock feed for that item.

**To investigate:** outlet ID, channel, the exact item(s), whether the store
shows online for that channel, and whether a POS stock feed is connected.
""",
    ),
    (
        "Orders are not reaching the POS / orders are missing",
        """\
# Orders not landing in the POS

A channel order flows: aggregator -> UrbanPiper -> **order webhook** to the
POS. Missing orders means one hop is broken:

1. **Is the order in UrbanPiper at all?** Business Manager -> Orders. If it's
   there but not in the POS, the webhook to the POS failed — check the POS
   integration's webhook logs and use the **order retry** action to resend.
2. **If it's not in UrbanPiper**, the aggregator never delivered it — check
   the channel is linked and online for that outlet, and that the outlet
   isn't paused on the aggregator side.
3. **POS offline / rejecting.** If the POS endpoint is down or 4xx-ing, the
   webhook retries a bounded number of times then stops. Bring the POS
   endpoint up and replay via order retry.

**To investigate:** outlet ID, channel, one example channel order ID + the
time it was placed, which POS should receive it, and whether that POS shows
online in UrbanPiper.
""",
    ),
    (
        "Store / outlet is showing offline on Swiggy or Zomato",
        """\
# Outlet shows offline on the channel

Store state is pushed via the **store toggle** action per outlet per channel.

1. **Check the toggle in Business Manager / Prime app.** If it shows online
   here but offline on the aggregator, re-send the toggle for that channel.
2. **Aggregator-side pause.** Swiggy/Zomato can pause an outlet for their own
   reasons (KPT breaches, doc issues, manual pause in their partner app).
   That state can't be overridden from UrbanPiper — the restaurant must
   un-pause in the aggregator partner app.
3. **Scheduled hours.** If the outlet has channel timings configured, it will
   go offline outside those hours automatically.
4. **A failed toggle call** does not retry — re-issue it.

**To investigate:** outlet ID, channel(s), what the toggle shows in Business
Manager, and whether the aggregator partner app shows a manual pause.
""",
    ),
    (
        "Menu publish failed or is stuck",
        """\
# Publish failed / stuck

Business Manager -> outlet -> **Publish Logs** is the source of truth.

- **Failed** with a validation error: the menu has something the channel
  rejects — an item with no price, no category, a bad image, a name over the
  channel's length limit, or a mapping gap. The log names the offending
  item; fix it and re-publish.
- **Stuck / in progress for a long time:** a large menu or an aggregator-side
  slowdown. Give it one cycle; if it's still not done, re-trigger a full
  publish for that single outlet rather than a bulk publish.
- **Partial:** some items/sections landed, others didn't — treat the failed
  items like a *Failed* publish above.
- Publishing repeatedly in quick succession can queue behind itself — wait
  for one to finish before starting the next.

**To investigate:** outlet ID, target channel(s), the exact error text/code
from the Publish Log or Error Report, and when publishing last worked.
""",
    ),
    (
        "A new item is not appearing on the channel",
        """\
# New item missing on Swiggy / Zomato

1. **Mapping.** A new item must be mapped to the channel for that outlet.
   Unmapped items are skipped on publish.
2. **Full publish.** Newly added or newly mapped items land on the next
   **full** menu publish for the outlet, not an incremental one.
3. **Category visibility.** If the item's category isn't mapped/enabled for
   the channel, the item won't show even if it published.
4. **Channel review.** Some aggregators hold brand-new items for a short
   review before they go live.

**To investigate:** outlet ID, channel, the item name, whether it's mapped to
the channel, and the last full-publish status for that outlet.
""",
    ),
    (
        "How do I link a Swiggy / Zomato outlet to UrbanPiper?",
        """\
# Linking an aggregator outlet

Channel linking is done per outlet in **Business Manager -> the outlet ->
Channels**. You provide the aggregator's outlet/restaurant ID for that
location; UrbanPiper maps it so menu publishes, store toggles and orders
flow for that channel.

- The aggregator ID must be the **store-level** ID, not the brand ID.
- After linking, run a **full menu publish** for the outlet so the channel
  gets the current menu.
- If orders still don't arrive after linking, confirm the aggregator has
  UrbanPiper set as the integration/POS partner for that store on their side.

**To investigate:** outlet ID, channel, the aggregator outlet ID entered, and
whether a full publish has been run since linking.
""",
    ),
    (
        "Order webhook to the POS is not firing / how does order retry work",
        """\
# POS order webhook not firing

Every channel order triggers an **order webhook** to the configured POS
endpoint. If the POS isn't receiving them:

1. **Endpoint reachable?** The POS URL must be publicly reachable and return
   2xx quickly. Timeouts and 5xx are treated as failures.
2. **Auth headers.** The webhook sends the agreed auth header(s); a 401/403
   from the POS stops delivery for that order.
3. **Retries are bounded.** A failed webhook retries a fixed number of times
   with backoff, then stops. Once the endpoint is healthy, use the
   **webhook order retry** action to resend specific orders.
4. **Check the webhook logs** for that outlet to see the response code the
   POS returned.

**To investigate:** outlet ID, the POS endpoint URL, an example order ID that
didn't arrive, and the response code shown in the webhook log.
""",
    ),
    (
        "An order is stuck as 'placed' and not moving to acknowledged",
        """\
# Order not progressing

Order state moves via **order status update** calls (from the POS or the
Prime app). Stuck at *placed* means no acknowledge was received:

1. **Prime app** — is the tablet online and logged in for that outlet? An
   offline tablet can't acknowledge.
2. **POS integration** — if the POS is meant to auto-acknowledge, check its
   integration is up and the order webhook actually reached it.
3. **Manual** — acknowledge from the Prime app / Business Manager to unblock,
   then investigate why the automatic path didn't fire.

An order left un-acknowledged past the aggregator's window may be
auto-cancelled by Swiggy/Zomato.

**To investigate:** outlet ID, channel, the order ID, whether the Prime
tablet is online, and whether a POS integration owns acknowledgement.
""",
    ),
    (
        "A discount or offer is not reflecting on the channel",
        """\
# Offer not showing on Swiggy / Zomato

- **UrbanPiper-managed offers** push on menu publish — confirm the offer is
  active, in date range, mapped to the outlet + channel, and that a publish
  ran after it was created.
- **Aggregator-managed offers** (created in the Swiggy/Zomato partner app)
  are not controlled by UrbanPiper at all — those must be checked and fixed
  on the aggregator side.
- Item-level vs cart-level: an item discount won't appear as a cart offer and
  vice versa.

**To investigate:** outlet ID, channel, the offer name/type, where it was
created (UrbanPiper or the aggregator app), and the last publish time.
""",
    ),
    (
        "Category timings are not applying on the channel",
        """\
# Category / timing groups not taking effect

Timing groups (e.g. breakfast-only categories) are attached to categories and
pushed on **menu publish**.

1. Confirm the timing group is assigned to the category **for that outlet**.
2. Confirm a **full publish** ran after the timing change.
3. Timezone — timing groups run in the outlet's configured timezone; a wrong
   outlet timezone makes items appear/disappear at the wrong local time.
4. The aggregator must support scheduled categories for the effect to show;
   otherwise the category is always on.

**To investigate:** outlet ID, channel, the category + timing group, the
outlet's timezone, and the last full-publish time.
""",
    ),
    (
        "How does menu sync / publish work in UrbanPiper?",
        """\
# The publish path (concept)

The menu is authored once in **Business Manager**. Changes — items, prices,
images, availability rules, categories, timing groups — are **not live** on
their own. They reach Swiggy, Zomato and other channels only when you
**Publish** for an outlet.

- **Full publish**: sends the entire current menu for the outlet to each
  linked channel. Required for new/newly-mapped items and images.
- **Incremental updates**: price and stock toggles can go out without a full
  publish, per item.
- **Publish Logs** (per outlet) record every publish, its result
  (Success / Partial / Failed) and the offending item on failure.
- **Error Report** lists validation problems the channel rejected.

If something looks wrong on a channel, the first question is always: *did a
publish succeed for that outlet after the change?*
""",
    ),
    (
        "Self-delivery rider status / live tracking is not updating",
        """\
# Rider status not updating (self-delivery)

For self-delivery outlets, rider assignment and location go to the channel
via **rider status update** and **rider live tracking** calls.

1. The order must be a **self-delivery** order for that outlet — marketplace
   (aggregator-delivered) orders ignore rider updates.
2. The rider app / integration must be sending status transitions
   (assigned -> picked up -> delivered) in order; a skipped state stalls the
   channel view.
3. Live tracking needs periodic location pings; if they stop, the map
   freezes at the last point.

**To investigate:** outlet ID, order ID, whether it's a self-delivery order,
and what the last rider status/ping received was.
""",
    ),
]


def _source_id(sb) -> str:
    rows = (sb.table("sources").select("source_id")
            .eq("tenant_id", TENANT).eq("name", SOURCE_NAME).execute().data)
    if rows:
        return rows[0]["source_id"]
    return (sb.table("sources").insert({
        "kind": "internal_kb", "tenant_id": TENANT, "name": SOURCE_NAME,
        "status": "active",
        "config": {"origin": "curated", "note": "UrbanPiper demo FAQ"},
    }).execute().data[0]["source_id"])


def seed() -> None:
    sb = get_supabase()
    sid = _source_id(sb)
    print(f"source {SOURCE_NAME} = {sid}")
    for title, body in ENTRIES:
        existing = (sb.table("kb_entries").select("entry_id")
                    .eq("source_id", sid).eq("title", title).execute().data)
        if existing:
            eid = existing[0]["entry_id"]
            sb.table("kb_entries").update({"body_md": body}).eq("entry_id", eid).execute()
        else:
            eid = (sb.table("kb_entries").insert({
                "source_id": sid, "tenant_id": TENANT, "title": title, "body_md": body,
            }).execute().data[0]["entry_id"])
        n = embed_entry(sb, source_id=sid, url=f"kb://{sid}/{eid}", title=title,
                        body_md=body, section=SECTION)
        sb.table("kb_entries").update({
            "chunk_count": n,
            "embed_hash": hashlib.md5(body.encode()).hexdigest(),
            "embedded_at": "now()",
        }).eq("entry_id", eid).execute()
        print(f"  {n:>2} chunks  {title}")
    print(f"\ndone: {len(ENTRIES)} FAQ entries for tenant {TENANT}")


def show() -> None:
    sb = get_supabase()
    rows = (sb.table("sources").select("source_id")
            .eq("tenant_id", TENANT).eq("name", SOURCE_NAME).execute().data)
    if not rows:
        print("(no urbanpiper-faq source yet)")
        return
    sid = rows[0]["source_id"]
    ents = (sb.table("kb_entries").select("title, chunk_count")
            .eq("source_id", sid).execute().data or [])
    for e in ents:
        print(f"  {e.get('chunk_count', '?'):>2}  {e['title']}")
    print(f"\n{len(ents)} entr(y/ies)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    show() if args.list else seed()


if __name__ == "__main__":
    main()
