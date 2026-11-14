-- Case Monitor: requests from agents who need a senior's help on a case, and
-- whether anyone has answered yet.

create table if not exists monitor_help_requests (
  tenant_id   uuid not null,
  ts          text not null,        -- the request's message id in its channel
  url         text,
  agent       text,
  ticket      text,
  module      text,
  sub_module  text,
  priority    text,
  question    text,
  answered_by text,
  answered_at timestamptz,
  raised_at   timestamptz not null,
  synced_at   timestamptz not null default now(),
  primary key (tenant_id, ts)
);
create index if not exists monitor_help_requests_raised_idx
  on monitor_help_requests (tenant_id, raised_at desc);

alter table monitor_help_requests enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'monitor_help_requests_member_read'
                 and polrelid = 'public.monitor_help_requests'::regclass) then
    create policy monitor_help_requests_member_read on monitor_help_requests
      for select to authenticated
      using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;
