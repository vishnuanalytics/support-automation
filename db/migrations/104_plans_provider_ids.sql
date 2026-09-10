-- Billing chunk C: each of our `plans` rows needs a matching Plan object on
-- the provider's side before a subscription can reference it (Razorpay and
-- Stripe both require a pre-created provider-side Plan/Price, not an
-- ad-hoc amount per subscription). Nullable — a plan with no real price
-- yet (today's seeded free/pro rows) simply has no provider plan until
-- pricing is signed off and someone creates one.

alter table plans
  add column if not exists razorpay_plan_id text,
  add column if not exists stripe_price_id text;

comment on column plans.razorpay_plan_id is
  'Razorpay Plan id (plan_...) this maps to — created once per real price '
  'point via the Razorpay dashboard or API, then stored here. Null until '
  'this plan has a real, signed-off price.';
comment on column plans.stripe_price_id is
  'Stripe Price id (price_...), same idea, for the Stripe provider (chunk B).';
