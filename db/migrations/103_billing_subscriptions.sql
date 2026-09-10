-- Billing chunk C: Razorpay subscriptions + the webhook idempotency ledger.
--
-- `subscriptions` mirrors whichever payment provider's own subscription
-- object is the source of truth (Razorpay first; Stripe's shape is close
-- enough to reuse this table when that chunk lands) — one row per tenant
-- while it has a *live* (non-terminal) subscription; a canceled/expired one
-- is kept for history, never hard-deleted, same discipline as every other
-- externally-sourced table in this project (zapier_docs.status,
-- missed_runs, kb_entries.status).
--
-- `billing_events` is the append-only webhook log: `(provider,
-- provider_event_id)` is the idempotency key (same pattern as
-- `jobs.dedupe_key`) so a webhook Razorpay/Stripe retries is applied once.

create table if not exists subscriptions (
  subscription_id         uuid primary key default gen_random_uuid(),
  tenant_id                uuid not null references tenants(tenant_id),
  plan_id                  uuid not null references plans(plan_id),
  provider                 text not null check (provider in ('stripe', 'razorpay')),
  provider_customer_id     text,
  provider_subscription_id text,
  status                   text not null default 'created',
    -- normalized: created | active | past_due | canceled | expired —
    -- handle_webhook_event() maps each provider's own status vocabulary
    -- onto this set; the provider's raw status also rides along in the
    -- matching billing_events row for anyone who needs the exact value.
  short_url                text,   -- Razorpay's hosted subscription page (or Stripe's Checkout/Portal URL)
  current_period_start     timestamptz,
  current_period_end       timestamptz,
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now()
);

-- at most one *live* subscription per tenant; canceled/expired rows are
-- history and don't block a fresh one.
create unique index if not exists subscriptions_tenant_live_uidx
  on subscriptions (tenant_id) where status not in ('canceled', 'expired');

create index if not exists subscriptions_provider_ref_idx
  on subscriptions (provider, provider_subscription_id);

alter table subscriptions enable row level security;
do $$ begin
  if not exists (select 1 from pg_policy where polname = 'subscriptions_tenant_read'
                 and polrelid = 'public.subscriptions'::regclass) then
    create policy subscriptions_tenant_read on subscriptions
      for select to authenticated using (public.is_tenant_member(tenant_id));
  end if;
end $$;
-- no write policy — service role only (the webhook handler + checkout-session
-- creation both run under _service, same as every payment-provider mutation).

create or replace function touch_subscription() returns trigger
language plpgsql set search_path = '' as $$
begin
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists trg_touch_subscription on subscriptions;
create trigger trg_touch_subscription before update on subscriptions
  for each row execute function touch_subscription();

-- ---- billing_events ---------------------------------------------------
create table if not exists billing_events (
  event_id          uuid primary key default gen_random_uuid(),
  tenant_id         uuid references tenants(tenant_id),
  provider          text not null check (provider in ('stripe', 'razorpay')),
  provider_event_id text not null,
  event_type        text,
  payload           jsonb not null default '{}'::jsonb,
  created_at        timestamptz not null default now(),
  unique (provider, provider_event_id)
);

create index if not exists billing_events_tenant_idx on billing_events (tenant_id, created_at desc);

-- RLS enabled, no policies at all -- service role (webhook receiver) only,
-- same lockdown as `jobs`. This is a raw provider-payload ledger, not
-- something a tenant needs to browse.
alter table billing_events enable row level security;

comment on table subscriptions is
  'One row per tenant per payment-provider subscription. Live (non-'
  'terminal) rows are unique per tenant; canceled/expired rows are kept '
  'as history, never hard-deleted.';
comment on table billing_events is
  'Raw webhook ledger, keyed by (provider, provider_event_id) for '
  'idempotency -- a retried webhook is applied once. Service-role only.';
