-- Case Monitor: saved data checks that new aggregator cases still need. Rewritten after
-- each sync from the auto_checks rules. A scheduled Claude routine, which can reach the
-- BI tool, reads it, runs each check and writes the result to monitor_case_checks.
-- Derived data: a row disappears once its check is saved or the case no longer needs it.

create table if not exists monitor_checks_due (
  tenant_id    uuid not null,
  case_id      text not null,         -- Salesforce case ID, as in monitor_cases
  kind         text not null,         -- an auto_checks kind, e.g. order, item_stock
  case_number  text,
  check_name   text not null,         -- the saved question's name
  card_id      text,                  -- the saved question's id in the BI tool
  params       jsonb not null default '{}'::jsonb,
  fill_from    jsonb,                 -- {"check": name, "params": {param: where to read it}}
  computed_at  timestamptz not null default now(),
  primary key (tenant_id, case_id, kind)
);

alter table monitor_checks_due enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'monitor_checks_due_member_read'
                 and polrelid = 'public.monitor_checks_due'::regclass) then
    create policy monitor_checks_due_member_read on monitor_checks_due
      for select to authenticated
      using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;
