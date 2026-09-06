-- Supabase advisor follow-up (the first pass was migration 027). Two real
-- issues + linter noise:
--
-- 1. **Destructive RPCs reachable by any signed-in user.** `purge_old*` all
--    DELETE rows and were granted EXECUTE to `anon` + `authenticated` — a
--    logged-in user could POST /rest/v1/rpc/purge_old_audit_log with
--    retain_days=0 and wipe the audit log. `claim_job(uuid)` (SECURITY
--    DEFINER) could let one claim/steal a queued job. All of these are
--    cron + service-role only (scripts/purge_old.py, api/worker.py, both
--    on the service key, which bypasses grants) — revoke the client roles.
-- 2. **Mutable search_path** on the purge_* / touch_case_taxonomy functions
--    (advisor 0011).
-- 3. Linter noise: an unindexed FK on `zapier_docs.source_id`; the
--    secret-holding / worker-internal tables that run RLS with no policy
--    (already deny-all to clients) get an explicit `using (false)` policy so
--    intent is on the record and the "RLS enabled, no policy" flag clears.
--    service_role bypasses RLS, so nothing functional changes.

alter function public.purge_old(integer, integer)                 set search_path = public, pg_temp;
alter function public.purge_old_audit_log(integer)                set search_path = public, pg_temp;
alter function public.purge_old_case_events(integer)              set search_path = public, pg_temp;
alter function public.purge_old_flow_versions(integer, integer)   set search_path = public, pg_temp;
alter function public.purge_old_reasoning_sessions(integer)       set search_path = public, pg_temp;
alter function public.touch_case_taxonomy()                       set search_path = public, pg_temp;

revoke execute on function public.purge_old(integer, integer)               from public, anon, authenticated;
revoke execute on function public.purge_old_audit_log(integer)              from public, anon, authenticated;
revoke execute on function public.purge_old_case_events(integer)            from public, anon, authenticated;
revoke execute on function public.purge_old_flow_versions(integer, integer) from public, anon, authenticated;
revoke execute on function public.purge_old_reasoning_sessions(integer)     from public, anon, authenticated;
revoke execute on function public.touch_case_taxonomy()                     from public, anon, authenticated;
revoke execute on function public.claim_job(uuid)                           from public, anon, authenticated;

create index if not exists idx_zapier_docs_source on public.zapier_docs (source_id);

do $$
declare t text;
begin
  foreach t in array array['connections', 'connection_actions', 'jobs',
                            'sf_cdc_state', 'tenant_integrations']
  loop
    if not exists (select 1 from pg_policy where polrelid = ('public.' || t)::regclass) then
      execute format(
        'create policy %I_no_client_access on public.%I for all to authenticated '
        'using (false) with check (false)', t, t);
    end if;
  end loop;
end $$;
