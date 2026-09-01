-- Phase 27a: the case coordination layer.
--
--  * case_events — an append-only, per-Case audit log. Every pipeline node,
--    every sweep action, every Slack button, and every Omni assignment writes
--    exactly one row. `/api/trace/<case>` folds this in as the spine so one
--    call answers "everything that ever happened to this case, and who did it".
--    Kept separate from `runs.trace` (which is one pipeline pass) — a case
--    accumulates many runs + sweep + Omni + Slack events across its life, and
--    this is retained longer than `runs` (purge_old trims runs at 60 days).
--
--  * sla_policy — the per-(tier, routed_team) ack / resolve thresholds the
--    queue_sweep job reads. Seed = ack 30 min everywhere; resolve 4 / 8 / 24 h
--    by tier (Phase 27 design decision).

create table if not exists case_events (
  event_id      bigint generated always as identity primary key,
  tenant_id     uuid not null,
  case_sf_id    text not null,
  case_number   text,
  ts            timestamptz not null default now(),
  actor         text not null,   -- ai | agent:<slack_uid> | system:sweep | system:cdc | system:omni
  action        text not null,   -- classify | route | gate | notify_human | handover |
                                 -- omni_route | omni_accept | send | reassign | breach | reconcile
  from_status   text,
  to_status     text,
  reason        text,
  routed_team   text,
  slack_channel text,
  slack_ts      text,
  run_id        uuid,
  confidence    numeric(3,2)
);

create index if not exists case_events_case_idx    on case_events (case_sf_id, ts);
create index if not exists case_events_tenant_ts_idx on case_events (tenant_id, ts);
create index if not exists case_events_run_idx     on case_events (run_id);

alter table case_events enable row level security;
create policy case_events_read on case_events for select to authenticated
  using (public.is_tenant_member(tenant_id));
create policy case_events_write on case_events for all to authenticated
  using (public.is_tenant_editor(tenant_id))
  with check (public.is_tenant_editor(tenant_id));


create table if not exists sla_policy (
  tier          text not null,   -- basic | premium | enterprise
  routed_team   text not null,   -- support | tier2 | csm | sales | offboarding | billing
  ack_minutes   int  not null default 30,
  resolve_hours int  not null default 24,
  primary key (tier, routed_team)
);

alter table sla_policy enable row level security;
create policy sla_policy_read on sla_policy for select to authenticated using (true);

insert into sla_policy (tier, routed_team, ack_minutes, resolve_hours) values
 ('basic','support',30,24),      ('premium','support',30,8),      ('enterprise','support',30,4),
 ('basic','tier2',30,24),        ('premium','tier2',30,8),        ('enterprise','tier2',30,4),
 ('basic','billing',30,8),       ('premium','billing',30,8),      ('enterprise','billing',30,4),
 ('basic','offboarding',30,24),  ('premium','offboarding',30,24), ('enterprise','offboarding',30,24),
 ('basic','csm',30,24),          ('premium','csm',30,24),         ('enterprise','csm',30,24),
 ('basic','sales',30,24),        ('premium','sales',30,24),       ('enterprise','sales',30,24)
on conflict (tier, routed_team) do nothing;
