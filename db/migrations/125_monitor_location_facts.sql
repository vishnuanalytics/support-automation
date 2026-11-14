-- Case Monitor: which platform ID belongs to which location, copied from the catalogue,
-- so a case's IDs can be checked by rule.

create table if not exists monitor_location_facts (
  tenant_id     uuid not null,
  location_id   text not null,
  platform      text not null,
  platform_id   text,
  biz_id        text,
  location_name text,
  synced_at     timestamptz not null default now(),
  primary key (tenant_id, location_id, platform)
);

alter table monitor_location_facts enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'monitor_location_facts_member_read'
                 and polrelid = 'public.monitor_location_facts'::regclass) then
    create policy monitor_location_facts_member_read on monitor_location_facts
      for select to authenticated
      using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;
