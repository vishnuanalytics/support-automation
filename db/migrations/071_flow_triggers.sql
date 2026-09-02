-- P6a (FR-46): externally-fired triggers for a flow.
--
-- A `webhook` trigger mints a URL secret; `POST /t/<token>` (no auth) turns an
-- arbitrary JSON body into a `context` run (P5b `triggers.webhook_context`).
-- A `schedule` trigger carries a 5-field cron; the `fire_schedules` worker
-- sweep (P6b) enqueues a run when it's due. Either way the flow reads its
-- input as `context.*` / `input.*` — no Salesforce Case involved.

create table if not exists flow_triggers (
  trigger_id    uuid primary key default gen_random_uuid(),
  flow_id       uuid not null references flows(flow_id) on delete cascade,
  tenant_id     uuid not null,
  kind          text not null default 'webhook',   -- 'webhook' | 'schedule'
  token         text unique,                       -- webhook: the URL secret
  cron          text,                              -- schedule: '*/15 * * * *'
  label         text,
  enabled       boolean not null default true,
  last_fired_at timestamptz,
  fire_count    bigint not null default 0,
  created_by    uuid,
  created_at    timestamptz not null default now()
);

create index if not exists idx_flow_triggers_flow on flow_triggers (flow_id);
create index if not exists idx_flow_triggers_schedule
  on flow_triggers (kind, enabled) where kind = 'schedule';

alter table flow_triggers enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'flow_triggers_tenant_rw'
                 and polrelid = 'public.flow_triggers'::regclass) then
    create policy flow_triggers_tenant_rw on flow_triggers
      for all to authenticated
      using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()))
      with check (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;

comment on table flow_triggers is
  'P6a: webhook / schedule triggers that start a flow on a generic `context` '
  'payload. The POST /t/<token> endpoint reads this via the service role.';
