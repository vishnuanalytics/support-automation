-- Post-Phase-16 hardening — clears the Supabase advisor warnings introduced
-- (or made visible) by phases 13–16. No behaviour change.
--
--   1. claim_job() was callable by anon / authenticated via /rest/v1/rpc.
--      Only the worker (service_role, which ignores grants) should run it.
--   2. The two `updated_at` trigger fns had a role-mutable search_path.
--   3. RLS policies on the phase 14–16 tables re-evaluated auth.uid() per
--      row; wrap in a scalar subquery so it's evaluated once.
--      (The older flows/flow_nodes/tenant_members/runs policies have the
--      same pattern — left for a separate pass to avoid touching the
--      editor's core RLS here.)

-- 1 ----------------------------------------------------------------------
-- PUBLIC holds the default EXECUTE grant (anon/authenticated inherit it),
-- so revoke from PUBLIC too.
revoke execute on function public.claim_job() from public, anon, authenticated;

-- 2 ----------------------------------------------------------------------
alter function public.touch_kb_entry()   set search_path = '';
alter function public.touch_policy_rule() set search_path = '';

-- 3 ----------------------------------------------------------------------
drop policy if exists sources_read on public.sources;
create policy sources_read on public.sources for select to authenticated
  using (tenant_id is null
         or tenant_id in (select tenant_id from tenant_members
                          where user_id = (select auth.uid())));

drop policy if exists sources_insert_internal_kb on public.sources;
create policy sources_insert_internal_kb on public.sources for insert to authenticated
  with check (kind = 'internal_kb'
    and tenant_id in (select tenant_id from tenant_members
                      where user_id = (select auth.uid())));

drop policy if exists sources_update_internal_kb on public.sources;
create policy sources_update_internal_kb on public.sources for update to authenticated
  using (kind = 'internal_kb'
    and tenant_id in (select tenant_id from tenant_members
                      where user_id = (select auth.uid())))
  with check (kind = 'internal_kb'
    and tenant_id in (select tenant_id from tenant_members
                      where user_id = (select auth.uid())));

drop policy if exists sources_delete_internal_kb on public.sources;
create policy sources_delete_internal_kb on public.sources for delete to authenticated
  using (kind = 'internal_kb'
    and tenant_id in (select tenant_id from tenant_members
                      where user_id = (select auth.uid())));

drop policy if exists kb_entries_tenant_rw on public.kb_entries;
create policy kb_entries_tenant_rw on public.kb_entries for all to authenticated
  using (tenant_id in (select tenant_id from tenant_members
                       where user_id = (select auth.uid())))
  with check (tenant_id in (select tenant_id from tenant_members
                            where user_id = (select auth.uid())));

drop policy if exists policy_rules_tenant_rw on public.policy_rules;
create policy policy_rules_tenant_rw on public.policy_rules for all to authenticated
  using (tenant_id in (select tenant_id from tenant_members
                       where user_id = (select auth.uid())))
  with check (tenant_id in (select tenant_id from tenant_members
                            where user_id = (select auth.uid())));

drop policy if exists action_requests_tenant_read on public.action_requests;
create policy action_requests_tenant_read on public.action_requests for select to authenticated
  using (tenant_id in (select tenant_id from tenant_members
                       where user_id = (select auth.uid())));
