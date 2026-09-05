-- Per-tenant case-taxonomy config: overrides for the module/submodule/
-- region/case-type keyword rules interpreter/case_taxonomy.py uses
-- (map_case_fields / map_case_type). Was a single hardcoded global dict --
-- see PROJECT_SCOPE.md "Scoped, not built: per-tenant case-taxonomy
-- config", now built.
--
-- One row per tenant, same single-row-per-tenant shape as `tenants`
-- (073_tenants.sql). A tenant with no row (the default, including every
-- tenant that existed before this migration) gets the code-side
-- DEFAULT_TAXONOMY unchanged -- `config` only needs to carry the keys a
-- tenant actually wants to override.

create table if not exists case_taxonomy (
  tenant_id  uuid primary key,
  config     jsonb not null default '{}'::jsonb,
  updated_by uuid,
  updated_at timestamptz not null default now()
);

alter table case_taxonomy enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'case_taxonomy_member_read'
                 and polrelid = 'public.case_taxonomy'::regclass) then
    -- read-only in the UI; writes go through PUT/DELETE
    -- /api/tenants/case-taxonomy (owner-gated, service-role upsert/delete) --
    -- no authenticated insert/update/delete policy on purpose, mirroring
    -- tenant_integrations.
    create policy case_taxonomy_member_read on case_taxonomy
      for select to authenticated
      using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;

comment on table case_taxonomy is
  'Per-tenant override of interpreter/case_taxonomy.py''s DEFAULT_TAXONOMY '
  '(module_rules/submodule_rules/region_by_country/case_type_rules). See '
  'that module''s docstring.';

create or replace function touch_case_taxonomy() returns trigger
language plpgsql as $$
begin new.updated_at = now(); return new; end $$;

drop trigger if exists trg_touch_case_taxonomy on case_taxonomy;
create trigger trg_touch_case_taxonomy before update on case_taxonomy
  for each row execute function touch_case_taxonomy();
