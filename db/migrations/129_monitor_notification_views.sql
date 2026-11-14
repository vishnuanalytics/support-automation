-- Case Monitor: when each person last marked a case's notification as viewed. A reply
-- after that time highlights the case again.

create table if not exists monitor_notification_views (
  tenant_id uuid not null,
  user_id   uuid not null,
  case_id   text not null,
  viewed_at timestamptz not null default now(),
  primary key (tenant_id, user_id, case_id)
);

alter table monitor_notification_views enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'monitor_notification_views_own_read'
                 and polrelid = 'public.monitor_notification_views'::regclass) then
    create policy monitor_notification_views_own_read on monitor_notification_views
      for select to authenticated
      using (user_id = auth.uid()
             and tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;
