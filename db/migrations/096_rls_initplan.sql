-- Supabase performance advisor 0003 (auth_rls_initplan): an RLS policy that
-- calls `auth.uid()` / `auth.jwt()` bare re-evaluates it **once per row**.
-- Wrapping it in a scalar subquery — `(select auth.uid())` — makes Postgres
-- evaluate it once per statement. Pure optimisation: the value is identical,
-- so every policy's semantics are unchanged; only the plan differs.
--
-- (The other perf lint, `multiple_permissive_policies` — a `FOR ALL` write
-- policy overlapping a separate `FOR SELECT` read policy on ~18 tables — is
-- NOT touched here: splitting each `FOR ALL` into 3 command-specific
-- policies is a large write-path RLS refactor for a lint that only bites at
-- scale, and an RLS mistake is dangerous. Left for a dedicated pass.)

alter policy case_taxonomy_member_read on public.case_taxonomy
  using (tenant_id in (select tenant_members.tenant_id from tenant_members
                       where tenant_members.user_id = (select auth.uid())));

alter policy flow_triggers_tenant_rw on public.flow_triggers
  using (tenant_id in (select tenant_members.tenant_id from tenant_members
                       where tenant_members.user_id = (select auth.uid())))
  with check (tenant_id in (select tenant_members.tenant_id from tenant_members
                            where tenant_members.user_id = (select auth.uid())));

alter policy graph_sync_state_read on public.graph_sync_state
  using (tenant_id in (select tenant_members.tenant_id from tenant_members
                       where tenant_members.user_id = (select auth.uid())));

alter policy handoff_watch_state_read on public.handoff_watch_state
  using (tenant_id in (select tenant_members.tenant_id from tenant_members
                       where tenant_members.user_id = (select auth.uid())));

alter policy review_tasks_tenant_read on public.review_tasks
  using (tenant_id in (select tenant_members.tenant_id from tenant_members
                       where tenant_members.user_id = (select auth.uid())));

alter policy tenant_isolation_runs on public.runs
  using (tenant_id in (select tenant_members.tenant_id from tenant_members
                       where tenant_members.user_id = (select auth.uid())))
  with check (tenant_id in (select tenant_members.tenant_id from tenant_members
                            where tenant_members.user_id = (select auth.uid())));

alter policy tenant_invitations_invitee_read on public.tenant_invitations
  using (status = 'pending'
         and lower(email::text) = lower(coalesce((select auth.jwt()) ->> 'email', '')));

alter policy tenant_invitations_owner_all on public.tenant_invitations
  using (exists (select 1 from tenant_members m
                 where m.tenant_id = tenant_invitations.tenant_id
                   and m.user_id = (select auth.uid()) and m.role = 'owner'))
  with check (exists (select 1 from tenant_members m
                      where m.tenant_id = tenant_invitations.tenant_id
                        and m.user_id = (select auth.uid()) and m.role = 'owner'));

alter policy self_membership_read on public.tenant_members
  using (user_id = (select auth.uid()));

alter policy tenants_member_read on public.tenants
  using (tenant_id in (select tenant_members.tenant_id from tenant_members
                       where tenant_members.user_id = (select auth.uid())));
