-- Case Monitor: support GitHub issues closed recently, with how they were labelled
-- and who raised them, so raises that needed no engineer can be coached.

create table if not exists monitor_closed_issues (
  tenant_id uuid not null,
  url       text not null,
  title     text,
  reporter  text,
  labels    jsonb not null default '[]'::jsonb,
  opened_at timestamptz,
  closed_at timestamptz,
  primary key (tenant_id, url)
);
create index if not exists monitor_closed_issues_closed_idx
  on monitor_closed_issues (tenant_id, closed_at desc);

alter table monitor_closed_issues enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'monitor_closed_issues_member_read'
                 and polrelid = 'public.monitor_closed_issues'::regclass) then
    create policy monitor_closed_issues_member_read on monitor_closed_issues
      for select to authenticated
      using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;
