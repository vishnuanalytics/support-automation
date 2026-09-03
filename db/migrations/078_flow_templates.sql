-- Phase 28, step 5 of 6: save-as-template.
--
-- interpreter/templates.py already serves 4 built-in, file-shipped
-- templates (P7a). This is the DB-backed counterpart: a user saves one of
-- their own flows as a reusable template, scoped to their own tenant --
-- private to that workspace, not cross-tenant (a materially bigger,
-- security-sensitive decision this codebase hasn't made anywhere else;
-- deliberately deferred).
--
-- Unlike audit_log / connections, this table isn't secret-bearing (just
-- node/edge JSON) so it gets the same RLS split flows itself uses
-- (migration 032): any member reads, only owner/editor writes -- via the
-- caller's own RLS-scoped client, not routed through service-role.

create table if not exists flow_templates (
  template_id uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null,
  name        text not null,
  category    text not null default 'Custom',
  description text,
  nodes       jsonb not null,
  edges       jsonb not null,
  created_by  uuid,
  created_at  timestamptz not null default now()
);

create index if not exists flow_templates_tenant_idx on flow_templates (tenant_id);

alter table flow_templates enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'flow_templates_read'
                 and polrelid = 'public.flow_templates'::regclass) then
    create policy flow_templates_read on flow_templates for select
      using (public.is_tenant_member(tenant_id));
  end if;
  if not exists (select 1 from pg_policy where polname = 'flow_templates_write'
                 and polrelid = 'public.flow_templates'::regclass) then
    create policy flow_templates_write on flow_templates for all
      using (public.is_tenant_editor(tenant_id))
      with check (public.is_tenant_editor(tenant_id));
  end if;
end $$;

comment on table flow_templates is
  'Phase 28 step 5: user-saved flow templates, private to the saving '
  'tenant. Counterpart to the 4 built-in file templates in '
  'interpreter/flows/templates/*.json (P7a).';
