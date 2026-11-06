-- Case Monitor alerts: each change event can explain itself, and each person
-- tracks what they've already seen.

alter table monitor_events add column if not exists detail text;

create table if not exists monitor_alert_seen (
  tenant_id uuid not null,
  user_id   uuid not null,
  seen_at   timestamptz not null default now(),
  seen_event_id bigint not null default 0,  -- compared by id, because clocks can disagree
  primary key (tenant_id, user_id)
);

alter table monitor_alert_seen enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'monitor_alert_seen_own_read'
                 and polrelid = 'public.monitor_alert_seen'::regclass) then
    create policy monitor_alert_seen_own_read on monitor_alert_seen
      for select to authenticated using (user_id = auth.uid());
  end if;
end $$;
