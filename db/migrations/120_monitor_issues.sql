-- Case Monitor: every open support GitHub issue, with what the tech team has said
-- on it. A sync re-reads an issue only when its thread_hash changes.

create table if not exists monitor_issues (
  tenant_id   uuid not null,
  url         text not null,
  is_open     boolean not null default true,
  thread_hash text,
  status      text not null,
  waiting_on  text,
  reason      text,
  method      text not null,   -- rule | llm | none
  synced_at   timestamptz not null default now(),
  primary key (tenant_id, url)
);

alter table monitor_issues enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'monitor_issues_member_read'
                 and polrelid = 'public.monitor_issues'::regclass) then
    create policy monitor_issues_member_read on monitor_issues
      for select to authenticated
      using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;
