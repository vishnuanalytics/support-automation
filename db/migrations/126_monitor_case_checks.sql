-- Case Monitor: results of saved data checks run on a case from a Claude session
-- (MCP tool save_check), kept so the case page and later chats can see them.
-- Append-only: a re-run adds a row, it never replaces one.

create table if not exists monitor_case_checks (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null,
  case_id     text not null,        -- Salesforce case ID, as in monitor_cases
  check_name  text not null,
  card_id     text,                 -- the saved question's id in the BI tool
  params      jsonb not null default '{}'::jsonb,
  row_count   integer,              -- rows the check returned, before the cap below
  summary     text,
  rows        jsonb not null default '[]'::jsonb,  -- at most 50 result rows
  saved_by    text,
  created_at  timestamptz not null default now()
);
create index if not exists monitor_case_checks_case_idx
  on monitor_case_checks (tenant_id, case_id, created_at desc);

alter table monitor_case_checks enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'monitor_case_checks_member_read'
                 and polrelid = 'public.monitor_case_checks'::regclass) then
    create policy monitor_case_checks_member_read on monitor_case_checks
      for select to authenticated
      using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;
