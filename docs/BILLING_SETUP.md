# Billing setup (Stripe + Razorpay)

This app has **one platform-level account per provider** — unlike
Salesforce/Slack/Google, where each tenant connects their own external
account, every tenant here subscribes *into* the platform's own Stripe or
Razorpay account. `interpreter/payments.py`'s `provider_for_country()`
picks which one a tenant bills through, once, at signup: `IN` → Razorpay
(UPI/netbanking/cards + its own GST handling), everywhere else → Stripe.

## The pricing model — read this before anything else

As of 2026-09-20, every paid plan is a **flat monthly fee for the
platform** — seats, flow slots, and features. It does **not** include any
LLM usage. Past the trial, a tenant supplies their own LLM API key (BYOK,
set in Connections) and that provider bills them directly for tokens,
completely separate from this app's invoice. `interpreter/billing.py`'s
`assert_not_locked` blocks every flow run for an `active`/`grace` tenant
with no key on file — that's enforced regardless of plan tier.

| Plan | Price/mo | Seats | Flows | Runs |
|---|---|---|---|---|
| Free (trial, 7 days) | $0 | — | — | 75, on the platform's own key |
| Basic | $29 | 3 | 5 | unlimited — bring your own key |
| Pro | $79 | 10 | 25 | unlimited — bring your own key |
| Advanced | $199 | unlimited | unlimited | unlimited — bring your own key |
| Enterprise | custom (sales-assisted) | unlimited | unlimited | unlimited — bring your own key |

All of this lives in the `plans` table (migration `112`) — this doc
covers wiring it up to actually take a payment, not the pricing itself.

## What "checkout_available" means

`GET /api/billing/plans` marks a plan `checkout_available: true` only if
it has a `stripe_price_id` **or** `razorpay_plan_id` set. Free and
Enterprise deliberately never get one (no self-serve checkout for
either). A plan with neither is real in the pricing table but **nobody
can actually subscribe to it** — the picker falls back to a "Talk to us"
button, and `POST /api/billing/subscribe` raises a clear `RuntimeError`
if called anyway (`interpreter/payments.py`'s `stripe_create_subscription`
/ `razorpay_create_subscription`).

## Setting it up: `scripts/billing_setup.py`

Creates the actual Stripe Price and Razorpay Plan objects for
Basic/Pro/Advanced from what's already in the `plans` table, and writes
the resulting IDs back onto `plans.stripe_price_id` /
`plans.razorpay_plan_id`. **The same script for test mode and going live**
— it doesn't know or care which mode the API keys are in, it just uses
whatever's in `.env`, and prints which mode it detected so a run is never
ambiguous about whether it just created real, billable objects.

```
python -m scripts.billing_setup               # do it
python -m scripts.billing_setup --dry-run      # show what would change first
python -m scripts.billing_setup --only stripe  # just one provider
```

Idempotent — a plan that already has a price id for a provider is
skipped for that provider. Both providers' Price/Plan objects are
**immutable**: re-running after an actual price change needs the old id
cleared by hand first (`update plans set stripe_price_id = null where
slug = '...'`), same as a real repricing needs a new object in either
dashboard too, not an edit.

## Env vars

All read directly from `.env` (see `.env.example`) — no config lives in
the database:

| Var | Test-mode value looks like | Notes |
|---|---|---|
| `STRIPE_SECRET_KEY` | `sk_test_...` | server-side, never exposed to the browser |
| `STRIPE_PUBLISHABLE_KEY` | `pk_test_...` | not currently used server-side (checkout is fully hosted — Stripe's own page, not Stripe.js) |
| `STRIPE_WEBHOOK_SECRET` | from the webhook's own "Signing secret" | **test-mode and live-mode secrets are different values**, even for the same endpoint URL |
| `STRIPE_CHECKOUT_SUCCESS_URL` / `_CANCEL_URL` | `http://localhost:5173/billing?checkout=success` | must point at the real deployed origin before going live — Stripe redirects a real customer here after they pay |
| `RAZORPAY_KEY_ID` / `_KEY_SECRET` | `rzp_test_...` | server-side |
| `RAZORPAY_ACCOUNT_ID` | — | Settings → Account & Settings in the Razorpay dashboard |
| `RAZORPAY_WEBHOOK_SECRET` | from the webhook's own setup | same test/live distinction as Stripe's |

## Webhooks — how a payment actually activates a tenant

`POST /api/hooks/stripe` and `POST /api/hooks/razorpay` both feed the
same `_apply_billing_webhook` (`api/main.py`) — signature-verified
against the raw body, idempotent on `(provider, provider_event_id)`. The
moment a webhook reports a subscription as `active`,
`tenants.billing_status` flips to `active` in that same request. **This
is not a polling or manual-review step** — a successful real-money charge
activates the tenant within the same second the provider's webhook
fires, same as a failed one starts the grace-period clock
(`past_due` → `grace`, `GRACE_DAYS` = 3 by default,
`SWEEP_GRACE_DAYS` env var to change it) or a cancellation locks it out
(`canceled`/`expired` → `canceled`, blocked unconditionally regardless of
any BYOK key on file — the subscription pays for the platform, the key
only ever paid for the LLM calls).

For this to work at all, each provider's dashboard needs the webhook
endpoint registered against your **real, publicly reachable** API URL —
`https://<your-api-host>/api/hooks/stripe` and `.../api/hooks/razorpay`
— which only works once the API is actually deployed somewhere with a
real domain (see `docs/DEPLOY.md` / `docs/DEPLOY_WEB_AND_API.md`), not
`localhost`.

## Going from test mode to real money — the actual checklist

Today (checked live, 2026-09-20): `STRIPE_SECRET_KEY` is `sk_test_...`,
`RAZORPAY_KEY_ID` is `rzp_test_...` — test mode only, no real account has
been created with either provider yet. To actually accept a real payment:

1. **Complete each provider's business verification** in their
   dashboard — bank account, KYC/company details. This is a real-world
   step only a human can do; nothing here can automate it.
2. **Swap the `.env` keys** for the live-mode equivalents
   (`sk_live_...`, `rzp_live_...`, etc.) once verification is approved.
3. **Re-run `python -m scripts.billing_setup`** against those live keys
   — it creates a fresh, real Price/Plan per self-serve tier (the
   test-mode ones created earlier are untouched, just orphaned) and
   writes the new live IDs onto `plans`.
4. **Update `STRIPE_CHECKOUT_SUCCESS_URL` / `_CANCEL_URL`** to the real
   production domain.
5. **Register both webhook endpoints** against the real API URL in each
   provider's live-mode dashboard, and set `STRIPE_WEBHOOK_SECRET` /
   `RAZORPAY_WEBHOOK_SECRET` to what *that* registration gives you — a
   live-mode secret is a different value from the test-mode one, even for
   the identical endpoint URL used in step 5's test-mode counterpart.

Steps 2, 4 and 5 need to land together — a live secret key paired with a
still-test-mode webhook secret (or the reverse) will silently misbehave
(charges succeed but the tenant never activates, or vice versa).

## Verifying it works, without spending real money

In test mode, both providers accept well-known test card/UPI numbers on
their own hosted checkout page (Stripe's `4242 4242 4242 4242`; Razorpay's
test UPI ID `success@razorpay`) — completing checkout with one fires a
real webhook through the exact same code path a live payment would, so
the whole loop (checkout → webhook → `billing_status` flip) is fully
testable today, before any real account exists.
