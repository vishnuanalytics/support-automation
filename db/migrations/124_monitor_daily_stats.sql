-- Case Monitor: one row of workload numbers per day, so progress can be compared
-- with where the lead started.

create table if not exists monitor_daily_stats (
  tenant_id  uuid not null,
  day        date not null,
  stats      jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now(),
  primary key (tenant_id, day)
);

alter table monitor_daily_stats enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'monitor_daily_stats_member_read'
                 and polrelid = 'public.monitor_daily_stats'::regclass) then
    create policy monitor_daily_stats_member_read on monitor_daily_stats
      for select to authenticated
      using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;
