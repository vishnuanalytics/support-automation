-- Phase 28: a generic platform activity/audit log.
--
-- `case_events` (062) is the closest existing thing, but it's scoped to a
-- Salesforce Case's lifecycle (case_sf_id, from_status/to_status,
-- routed_team, ...). This is the platform-admin equivalent: who published /
-- rolled back / deleted a flow, removed a member, approved a KB change,
-- added/removed a connection. Same conventions as case_events (append-only,
-- a free-text `action` slug -- not an enum, matching the node-type
-- philosophy) and review_tasks (065) -- RLS is member-read only, no write
-- policy at all; every insert goes through the service-role client from
-- interpreter/audit.py, right after the mutation it records succeeds.

create table if not exists audit_log (
  event_id    bigint generated always as identity primary key,
  tenant_id   uuid not null,
  actor_id    uuid,             -- auth user id; null for a system/sweep action
  actor_email text,             -- snapshot at write time (survives a later-removed member)
  action      text not null,    -- e.g. "flow.published", "member.removed"
  target_type text,             -- "flow" | "member" | "connection" | "action_request" | ...
  target_id   text,
  summary     text,
  metadata    jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now()
);

create index if not exists audit_log_tenant_ts_idx on audit_log (tenant_id, created_at desc);

alter table audit_log enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'audit_log_tenant_read'
                 and polrelid = 'public.audit_log'::regclass) then
    create policy audit_log_tenant_read on audit_log
      for select to authenticated
      using (public.is_tenant_member(tenant_id));
  end if;
end $$;

comment on table audit_log is
  'Phase 28: platform activity log. Read: any tenant member. Write: '
  'service-role only, via interpreter/audit.py::record() -- best-effort, '
  'never blocks the mutation it records.';
