-- Billing foundation: `plans` become data (not `tenants.plan`'s two-value
-- CHECK-constrained enum, matching the project's own "node types are
-- generic strings, not an enum" rule applied to pricing tiers), plus the
-- tenant-level columns a real payment gateway needs: trial clock, billing
-- status, which provider/country a tenant bills through.
--
-- Scope of this migration: schema + backfill only. No payment processing
-- is wired to any of this yet (that's the next chunks — a Stripe/Razorpay
-- provider registry, then the trial-lifecycle sweep that actually reads
-- billing_status/trial_ends_at/grace_ends_at to warn or lock). The two
-- existing plans (free/pro) are seeded with their current real numbers so
-- this is a pure refactor of where the numbers live, not a behavior change.

-- ---- plans ----------------------------------------------------------------
create table if not exists plans (
  plan_id             uuid primary key default gen_random_uuid(),
  slug                text not null unique,             -- 'free' | 'pro' | ... — data, not an enum
  name                text not null,
  included_runs       int,                               -- null = unlimited
  included_tokens     int,                               -- null = unlimited
  base_price_usd      int not null default 0,            -- minor units (cents); 0 until real pricing lands
  base_price_inr      int not null default 0,            -- minor units (paise)
  overage_per_run_usd int not null default 0,            -- minor units, charged per run past included_runs
  byok_discount_pct   int not null default 0,            -- % off base_price_* when the tenant supplies its own LLM key
  seats_included      int,                                -- null = unlimited
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

comment on table plans is
  'The pricing catalog — a plan a tenant can be on. Admin/service-role '
  'managed (no self-serve plan creation). Replaces the old '
  'tenants.plan text CHECK(''free'',''pro'') enum.';

insert into plans (slug, name, included_runs, included_tokens)
values ('free', 'Free', 200, 500000),
       ('pro', 'Pro', null, null)
on conflict (slug) do nothing;

alter table plans enable row level security;
do $$ begin
  if not exists (select 1 from pg_policy where polname = 'plans_read'
                 and polrelid = 'public.plans'::regclass) then
    create policy plans_read on plans for select to authenticated using (true);
  end if;
end $$;

create or replace function touch_plan() returns trigger
language plpgsql set search_path = '' as $$
begin
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists trg_touch_plan on plans;
create trigger trg_touch_plan before update on plans
  for each row execute function touch_plan();

-- ---- tenants: trial + billing-provider columns -----------------------------
-- (a column DEFAULT can't be a subquery in Postgres, so the "new tenant
-- defaults to the free plan" behavior is a trigger instead, below)
alter table tenants
  add column if not exists plan_id uuid references plans(plan_id),
  add column if not exists billing_status text not null default 'trialing',
  add column if not exists billing_country text,
  add column if not exists payment_provider text,
  add column if not exists provider_customer_id text,
  add column if not exists trial_ends_at timestamptz,
  add column if not exists grace_ends_at timestamptz;

create or replace function default_tenant_plan() returns trigger
language plpgsql set search_path = '' as $$
begin
  if new.plan_id is null then
    new.plan_id = (select plan_id from public.plans where slug = 'free');
  end if;
  return new;
end $$;

drop trigger if exists trg_default_tenant_plan on tenants;
create trigger trg_default_tenant_plan before insert on tenants
  for each row execute function default_tenant_plan();

alter table tenants
  drop constraint if exists tenants_billing_status_check;
alter table tenants
  add constraint tenants_billing_status_check
    check (billing_status in ('trialing', 'active', 'grace', 'locked', 'canceled'));

alter table tenants
  drop constraint if exists tenants_payment_provider_check;
alter table tenants
  add constraint tenants_payment_provider_check
    check (payment_provider is null or payment_provider in ('stripe', 'razorpay'));

-- backfill: every *existing* tenant predates the trial concept entirely —
-- treat them as already active on whatever plan.'free'/'pro' they had,
-- never as freshly "trialing" with no trial_ends_at. Only a tenant created
-- after this migration (via POST /api/tenants) gets a real trial clock.
update tenants set plan_id = (select plan_id from plans where slug = tenants.plan)
where plan_id is null;
update tenants set billing_status = 'active' where billing_status = 'trialing';

alter table tenants alter column plan_id set not null;

alter table tenants drop constraint if exists tenants_plan_check;
alter table tenants drop column if exists plan;

comment on column tenants.billing_status is
  'trialing (7-day clock running) -> active (paid or grandfathered) '
  '-> grace (trial lapsed, 3-day warning window) -> locked (flow runs '
  'blocked at the job-claim gate) -> canceled. A card added at any point '
  'moves trialing/grace/locked straight to active.';
comment on column tenants.billing_country is
  'ISO billing country, set once at signup — decides payment_provider '
  '(India -> razorpay, else -> stripe).';
