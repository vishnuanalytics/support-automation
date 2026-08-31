-- Phase 20k: a per-tenant "this is the flow the Salesforce Case hook runs".
--
-- Before: POST /api/hooks/salesforce/case hard-resolved the flow by
-- `team = 'router' and status = 'published'` -- the one router flow was the
-- only possible entry point, and deleting/renaming it broke the hook with a
-- 500. Now any published flow can be marked `sf_entry`; the hook resolves
-- that one. Exactly one per tenant (partial-unique), so the resolution stays
-- unambiguous -- the API clears the flag on the tenant's other flows before
-- setting it on a new one.
--
-- Single concern: the flag + its uniqueness + a behaviour-preserving
-- backfill of the current router flow. RLS is unchanged -- `sf_entry` lives
-- on `flows`, already member-SELECT / editor-write per migration 032.

alter table flows
  add column if not exists sf_entry boolean not null default false;

-- At most one Salesforce-connected flow per tenant.
create unique index if not exists uq_one_sf_entry_flow_per_tenant
  on flows (tenant_id)
  where sf_entry;

-- Backfill: the flow the hook resolves today (the published 'router' flow)
-- keeps being the entry point, so this migration changes no behaviour.
update flows
   set sf_entry = true
 where status = 'published'
   and team = 'router'
   and not exists (
     select 1 from flows f2
      where f2.tenant_id = flows.tenant_id and f2.sf_entry
   );
