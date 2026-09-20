-- Billing simplification: BYOK becomes the ONE ongoing billing method,
-- replacing the three-mechanism stack chunk E/F built on top of run/token
-- metering (a checkout-time BYOK discount, a BYOK-vs-platform token split
-- excluded from the plan quota, and a BYOK-aware trial-cap exemption).
-- Product decision (2026-09-20): a tenant always supplies their own LLM
-- key once they're past the trial — the platform never bills for tokens
-- again — so a paid plan is a flat monthly fee for seats/flows/features,
-- not a metered quota with a BYOK discount bolted on.
--
-- The trial itself is unchanged in *mechanism* (still `free` plan +
-- `billing_status='trialing'` + interpreter/billing.py's
-- `_trial_cap_status`/`assert_not_locked`) — only its numbers change: a
-- flat 75-run cap on the platform's own key, tokens uncapped, so a trial
-- is "try it free for a while," not a token-budget exercise.

-- ---- retire the token/run metering + BYOK-discount columns -------------
-- Any tenant on the old placeholder 'pro' plan (102_billing_foundation.sql
-- — unlimited, never had a real price) moves onto 'growth' first so that
-- slug renamed to 'pro' below (see the real tier renames further down)
-- doesn't collide with a still-referenced row.
update tenants set plan_id = (select plan_id from plans where slug = 'growth')
where plan_id = (select plan_id from plans where slug = 'pro');
update subscriptions set plan_id = (select plan_id from plans where slug = 'growth')
where plan_id = (select plan_id from plans where slug = 'pro');
delete from plans where slug = 'pro';

alter table plans
  add column if not exists included_flows int,               -- null = unlimited active (non-archived) flows
  add column if not exists features       jsonb not null default '[]'::jsonb;  -- feature flags this tier unlocks

comment on column plans.included_flows is
  'Max non-archived flows this plan allows -- enforced at POST /api/flows '
  '(interpreter.billing.assert_flow_slot_available). Null = unlimited.';
comment on column plans.features is
  'Feature flags this tier unlocks (e.g. "policy_rules", "kb_writeback", '
  '"agentic_actions", "priority_support") -- informational/display only '
  'today, not yet enforced at the node/endpoint level.';

alter table plans
  drop column if exists byok_discount_pct,
  drop column if exists overage_per_run_usd,
  drop column if exists razorpay_plan_id_byok,
  drop column if exists stripe_price_id_byok;

-- ---- the trial plan: flat run cap, uncapped tokens ---------------------
update plans set included_runs = 75, included_tokens = null where slug = 'free';

-- ---- rename the real tiers to Basic / Pro / Advanced, flat + unmetered -
-- Renaming in place (not delete+insert) keeps existing subscriptions/
-- tenants.plan_id foreign keys intact -- a real Razorpay/Stripe plan
-- object still needs re-pointing by hand (plans.razorpay_plan_id /
-- stripe_price_id) once the new prices are created provider-side.
update plans set
  slug = 'basic', name = 'Basic',
  included_runs = null, included_tokens = null,   -- unmetered -- BYOK covers the LLM cost
  base_price_usd = 2900, base_price_inr = 240000,  -- $29/mo, ~INR 2,400/mo
  seats_included = 3, included_flows = 5,
  features = '["core"]'::jsonb
where slug = 'starter';

update plans set
  slug = 'pro', name = 'Pro',
  included_runs = null, included_tokens = null,
  base_price_usd = 7900, base_price_inr = 660000,  -- $79/mo, ~INR 6,600/mo
  seats_included = 10, included_flows = 25,
  features = '["core", "policy_rules", "kb_writeback"]'::jsonb
where slug = 'growth';

insert into plans (slug, name, included_runs, included_tokens, base_price_usd, base_price_inr,
                    seats_included, included_flows, features)
values (
  'advanced', 'Advanced', null, null, 19900, 1660000,  -- $199/mo, ~INR 16,600/mo
  null, null,   -- unlimited seats + flows
  '["core", "policy_rules", "kb_writeback", "agentic_actions", "priority_support"]'::jsonb
)
on conflict (slug) do nothing;

-- 'enterprise' (sales-assisted, unlimited, no self-serve checkout) is
-- untouched -- a separate mechanism from these three, not one of them.
