-- Case Monitor (FR-52): one queue showing whose turn it is on every open case.
--
-- Everything here is a tenant-scoped cache of data read from external systems,
-- plus local review state. Members read through RLS; writes go through the API
-- and worker with the service role, the same shape as case_taxonomy (086).
-- Nothing here holds a credential: Slack user tokens live in Vault.

create table if not exists monitor_config (
  tenant_id  uuid primary key,
  config     jsonb not null default '{}'::jsonb,
  updated_by uuid,
  updated_at timestamptz not null default now()
);

create table if not exists monitor_cases (
  tenant_id        uuid not null,
  case_id          text not null,
  source           text not null default 'salesforce',  -- salesforce | ai | demo
  is_open          boolean not null default true,        -- soft close, never deleted by sync
  case_data        jsonb not null,
  verdict          jsonb not null,
  timeline         jsonb,
  timeline_at      timestamptz,
  synced_at        timestamptz not null default now(),
  primary key (tenant_id, case_id)
);
create index if not exists monitor_cases_open_idx on monitor_cases (tenant_id, is_open);

create table if not exists monitor_evidence (
  tenant_id  uuid not null,
  url        text not null,
  data       jsonb not null,
  fetched_at timestamptz not null default now(),
  primary key (tenant_id, url)
);

create table if not exists monitor_resolved (
  tenant_id       uuid not null,
  case_id         text not null,
  case_number     text,
  subject         text,
  module          text,
  sub_module      text,
  third_parties   jsonb not null default '[]'::jsonb,
  opened_at       timestamptz,
  closed_at       timestamptz,
  resolution_note text,
  url             text,
  primary key (tenant_id, case_id)
);
create index if not exists monitor_resolved_module_idx
  on monitor_resolved (tenant_id, module, closed_at desc);

create table if not exists monitor_reviews (
  tenant_id    uuid not null,
  case_id      text not null,
  reviewed_at  timestamptz,
  reviewed_by  uuid,
  snooze_until timestamptz,
  snoozed_at   timestamptz,
  primary key (tenant_id, case_id)
);

create table if not exists monitor_syncs (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null,
  trigger     text not null,
  status      text not null default 'running',  -- running | ok | partial | failed
  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  counts      jsonb not null default '{}'::jsonb,
  errors      jsonb not null default '[]'::jsonb
);
create index if not exists monitor_syncs_tenant_idx on monitor_syncs (tenant_id, started_at desc);
-- The sync lock: a tenant can only ever have one running row.
create unique index if not exists monitor_syncs_one_running
  on monitor_syncs (tenant_id) where status = 'running';

create table if not exists monitor_events (
  id          bigint generated always as identity primary key,
  tenant_id   uuid not null,
  sync_id     uuid,
  case_id     text not null,
  case_number text,
  subject     text,
  kind        text not null,  -- new_case | new_reply | newly_overdue | newly_missing_link
  created_at  timestamptz not null default now()
);
create index if not exists monitor_events_tenant_idx on monitor_events (tenant_id, created_at desc);

do $$
declare t text;
begin
  foreach t in array array['monitor_config', 'monitor_cases', 'monitor_evidence',
                           'monitor_resolved', 'monitor_reviews', 'monitor_syncs',
                           'monitor_events']
  loop
    execute format('alter table %I enable row level security', t);
    if not exists (select 1 from pg_policy where polname = t || '_member_read'
                   and polrelid = format('public.%I', t)::regclass) then
      execute format(
        'create policy %I on %I for select to authenticated using (tenant_id in '
        '(select tenant_id from tenant_members where user_id = auth.uid()))',
        t || '_member_read', t);
    end if;
  end loop;
end $$;
