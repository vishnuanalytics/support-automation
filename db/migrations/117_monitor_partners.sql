-- Case Monitor: each tenant's directory of third parties. It says which channel
-- a case is raised on, so the rules know what evidence to expect, and who to
-- contact. No portal passwords here: those stay out of the database.

create table if not exists monitor_partners (
  tenant_id            uuid not null,
  name                 text not null,    -- matches the case system's value
  aliases              jsonb not null default '[]'::jsonb,
  channel              text not null,    -- email | slack | jira | portal | whatsapp
  to_emails            jsonb not null default '[]'::jsonb,
  cc_emails            jsonb not null default '[]'::jsonb,
  email_domains        jsonb not null default '[]'::jsonb,
  contacts             jsonb not null default '[]'::jsonb,
  portal_url           text,
  raise_template       text,
  escalate_after_hours numeric,
  escalation_note      text,
  notes                text,
  source               text,
  updated_at           timestamptz not null default now(),
  primary key (tenant_id, name)
);

alter table monitor_partners enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'monitor_partners_member_read'
                 and polrelid = 'public.monitor_partners'::regclass) then
    create policy monitor_partners_member_read on monitor_partners
      for select to authenticated
      using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;
