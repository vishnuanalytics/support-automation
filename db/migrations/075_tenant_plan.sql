-- P9: usage & billing dashboard.
--
-- A static plan name per tenant, checked against `interpreter/billing.py`'s
-- PLAN_LIMITS to compute quota usage. No payment processing lives behind
-- this column yet -- it's an admin-set label (default 'free'), not a
-- billing-system-driven value.

alter table tenants
  add column if not exists plan text not null default 'free';

alter table tenants
  drop constraint if exists tenants_plan_check;
alter table tenants
  add constraint tenants_plan_check check (plan in ('free', 'pro'));

comment on column tenants.plan is
  'Static plan name looked up in interpreter/billing.py PLAN_LIMITS. '
  'No payment processing behind this yet -- P9 scope is usage metering only.';
