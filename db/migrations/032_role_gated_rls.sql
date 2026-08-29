-- Phase 18b: role-gated writes.
--
-- `tenant_members.role` is 'owner' | 'editor' | 'viewer' (the column has
-- always existed; every current row is 'owner'). Until now RLS only checked
-- *membership* — any member could write. This splits every editable
-- tenant-scoped table into:
--   * SELECT  — any member of the tenant
--   * INSERT / UPDATE / DELETE — only role in ('owner','editor')
--
-- Mirrors 002_rls_and_constraints.sql's tenant-isolation pattern; the API
-- also does a `_require_editor` pre-check for a clean 403 (RLS is the
-- backstop, incl. for the SECURITY INVOKER `replace_flow_graph` RPC).

-- ---- helpers --------------------------------------------------------------
create or replace function public.is_tenant_member(tid uuid)
returns boolean language sql stable
set search_path = public, pg_temp as $$
  select exists (
    select 1 from tenant_members
    where tenant_id = tid and user_id = auth.uid()
  );
$$;

create or replace function public.is_tenant_editor(tid uuid)
returns boolean language sql stable
set search_path = public, pg_temp as $$
  select exists (
    select 1 from tenant_members
    where tenant_id = tid and user_id = auth.uid()
      and role in ('owner', 'editor')
  );
$$;

-- ---- flows -------------------------------------------------------------
drop policy if exists tenant_isolation_flows on flows;
create policy flows_read  on flows for select
  using (public.is_tenant_member(tenant_id));
create policy flows_write on flows for all
  using (public.is_tenant_editor(tenant_id))
  with check (public.is_tenant_editor(tenant_id));

-- ---- flow_nodes / flow_edges / flow_versions (keyed by flow_id) -------
do $$
declare t text;
begin
  foreach t in array array['flow_nodes', 'flow_edges', 'flow_versions'] loop
    execute format('drop policy if exists tenant_isolation_%1$s on %1$s', t);
    execute format($f$
      create policy %1$s_read on %1$s for select
        using (exists (select 1 from flows f
                       where f.flow_id = %1$s.flow_id
                         and public.is_tenant_member(f.tenant_id)))
    $f$, t);
    execute format($f$
      create policy %1$s_write on %1$s for all
        using (exists (select 1 from flows f
                       where f.flow_id = %1$s.flow_id
                         and public.is_tenant_editor(f.tenant_id)))
        with check (exists (select 1 from flows f
                            where f.flow_id = %1$s.flow_id
                              and public.is_tenant_editor(f.tenant_id)))
    $f$, t);
  end loop;
end $$;

-- ---- policy_rules ----------------------------------------------------
drop policy if exists policy_rules_tenant_rw on policy_rules;
create policy policy_rules_read  on policy_rules for select to authenticated
  using (public.is_tenant_member(tenant_id));
create policy policy_rules_write on policy_rules for all to authenticated
  using (public.is_tenant_editor(tenant_id))
  with check (public.is_tenant_editor(tenant_id));

-- ---- kb_entries ----------------------------------------------------
drop policy if exists kb_entries_tenant_rw on kb_entries;
create policy kb_entries_read  on kb_entries for select to authenticated
  using (public.is_tenant_member(tenant_id));
create policy kb_entries_write on kb_entries for all to authenticated
  using (public.is_tenant_editor(tenant_id))
  with check (public.is_tenant_editor(tenant_id));

-- ---- sources (internal_kb collections only; shared sources stay
--      service-role-managed via `sources_read` from 015) --------------
drop policy if exists sources_insert_internal_kb on sources;
drop policy if exists sources_update_internal_kb on sources;
drop policy if exists sources_delete_internal_kb on sources;
create policy sources_write_internal_kb on sources for all to authenticated
  using (kind = 'internal_kb' and public.is_tenant_editor(tenant_id))
  with check (kind = 'internal_kb' and public.is_tenant_editor(tenant_id));
