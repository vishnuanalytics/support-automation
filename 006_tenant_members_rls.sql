-- Phase 2 gap-closing: give tenant_members a real RLS policy.
--
-- 002 enabled RLS on tenant_members but never added a policy, so under a
-- normal authenticated session the table returns nothing -- and the
-- flows / flow_nodes / flow_edges policies all sub-select from it, so a
-- user couldn't see their own flows. It only worked so far because every
-- caller has been the service role (which bypasses RLS).
--
-- Phase 2's interpreter still runs as the service role, but the moment a
-- Phase 4/5 auth'd client loads a flow this becomes load-bearing. Add the
-- minimal policy now: a user may read (only) their own membership rows.
-- Writes stay service-role only (invite/onboarding flow, not built yet).

alter table tenant_members enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policy
    where polname = 'self_membership_read'
      and polrelid = 'public.tenant_members'::regclass
  ) then
    create policy self_membership_read on tenant_members
      for select
      to authenticated
      using (user_id = auth.uid());
  end if;
end $$;
