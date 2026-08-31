-- Phase 23: a heartbeat table. Each long-running process (worker / poller /
-- cdc) upserts its row every ~20s via interpreter.health.beat();
-- scripts/health_check.py reads it and alerts if a component goes quiet or
-- the job failure rate spikes.
--
-- Not tenant-scoped (infra, not customer data): readable by any authenticated
-- user, written only by the service role.

create table if not exists system_health (
  component        text primary key,          -- 'worker' | 'poller' | 'cdc' | 'api'
  last_healthy_at  timestamptz not null,
  detail           jsonb not null default '{}'::jsonb,
  updated_at       timestamptz not null default now()
);

alter table system_health enable row level security;
create policy system_health_read on system_health for select to authenticated using (true);
