-- Phase 4 RLS check — run in the Supabase SQL editor (or psql as a superuser).
--
-- Simulates three authenticated users by setting the JWT `sub` claim and the
-- `authenticated` role, then runs the SAME query each time. The `flows` RLS
-- policy (002) gates on `tenant_members` via auth.uid(); `tenant_members`
-- itself is readable per-user via the `self_membership_read` policy (006).
--
-- Expected (with the 009 seed):
--   4ddf2413… (Acme  member) -> 2 flows: support + offboarding, tenant 0000…
--   b2b20000… (Globex member) -> 1 flow:  support, tenant 2222…  + only its 8 nodes
--   99999999… (no membership) -> 0 flows / 0 nodes / 0 edges / 0 memberships
--   (service role / postgres) -> all 3 flows (BYPASSRLS)

-- ── Acme user ──────────────────────────────────────────────────────────
begin;
  set local role authenticated;
  select set_config('request.jwt.claims',
    '{"sub":"4ddf2413-6ccc-4c5e-9da6-b5c3c0391941","role":"authenticated"}', true);
  select 'acme user' as who,
         string_agg(team || '/' || left(tenant_id::text, 8), ', ' order by team) as flows
  from flows;
rollback;

-- ── Globex user ───────────────────────────────────────────────────────
begin;
  set local role authenticated;
  select set_config('request.jwt.claims',
    '{"sub":"b2b20000-0000-4000-8000-000000000002","role":"authenticated"}', true);
  select 'globex user' as who,
         string_agg(team || '/' || left(tenant_id::text, 8), ', ' order by team) as flows,
         (select count(*) from flow_nodes) as visible_nodes
  from flows;
rollback;

-- ── stranger ──────────────────────────────────────────────────────────
begin;
  set local role authenticated;
  select set_config('request.jwt.claims',
    '{"sub":"99999999-9999-4999-8999-999999999999","role":"authenticated"}', true);
  select 'no membership' as who,
         (select count(*) from flows)          as flows,
         (select count(*) from flow_nodes)     as nodes,
         (select count(*) from flow_edges)     as edges,
         (select count(*) from tenant_members) as memberships;
rollback;
