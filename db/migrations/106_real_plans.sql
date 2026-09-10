-- Billing chunk F: real, signed-off plan tiers (Starter/Growth/Enterprise),
-- replacing the free/pro placeholders as what a tenant actually subscribes
-- to. Numbers are the illustrative figures from the billing plan the user
-- approved (2026-09-10) — a starting price, not a permanent one; adjusting
-- them later is a data update, not a schema change.
--
-- Each priced tier gets TWO provider-side objects: a standard price and a
-- BYOK-discounted one (interpreter/llm.py::tenant_has_byok() decides which
-- one /api/billing/subscribe uses at checkout time) — hence the *_byok
-- columns alongside the existing razorpay_plan_id/stripe_price_id ones.
-- Real Stripe/Razorpay ids are backfilled by a follow-up UPDATE once the
-- Product/Price/Plan objects are created against each provider's test-mode
-- API (not something a plain SQL migration can do).

alter table plans
  add column if not exists razorpay_plan_id_byok text,
  add column if not exists stripe_price_id_byok text;

comment on column plans.razorpay_plan_id_byok is
  'Same idea as razorpay_plan_id, at the byok_discount_pct-reduced price -- '
  'used instead of razorpay_plan_id when the subscribing tenant already has '
  'their own LLM key configured (interpreter.llm.tenant_has_byok).';
comment on column plans.stripe_price_id_byok is
  'Stripe counterpart of razorpay_plan_id_byok.';

insert into plans (slug, name, included_runs, included_tokens, base_price_usd,
                    base_price_inr, overage_per_run_usd, byok_discount_pct, seats_included)
values
  -- $49/mo, ~INR 4,100/mo; 20% off with a BYOK key
  ('starter', 'Starter', 1000, 1500000, 4900, 410000, 6, 20, 3),
  -- $199/mo, ~INR 16,600/mo; 25% off with a BYOK key
  ('growth', 'Growth', 6000, 9000000, 19900, 1660000, 4, 25, 10),
  -- sales-assisted, not self-serve checkout -- no provider price object;
  -- an owner is placed on this plan by hand once a contract is signed.
  ('enterprise', 'Enterprise', null, null, 0, 0, 0, 0, null)
on conflict (slug) do nothing;
