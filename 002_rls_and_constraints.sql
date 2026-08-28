-- Phase 0 gap-closing: tenant isolation (RLS) + one published flow per team

-- Maps auth.users -> tenant_id. In a real multi-tenant setup this is
-- populated on signup/invite; for the PoC, insert rows manually.
create table if not exists tenant_members (
  user_id    uuid not null references auth.users(id) on delete cascade,
  tenant_id  uuid not null,
  role       text not null default 'member',   -- 'owner' | 'admin' | 'member'
  primary key (user_id, tenant_id)
);

-- Only one published flow per (tenant, team) at a time. Multiple drafts
-- are fine; multiple published flows for the same team are not --
-- Phase 2's interpreter needs a single unambiguous flow to load.
create unique index if not exists uq_one_published_flow_per_team
  on flows (tenant_id, team)
  where status = 'published';

alter table flows enable row level security;
alter table flow_nodes enable row level security;
alter table flow_edges enable row level security;

create policy tenant_isolation_flows on flows
  for all
  using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()))
  with check (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));

create policy tenant_isolation_flow_nodes on flow_nodes
  for all
  using (flow_id in (
    select flow_id from flows
    where tenant_id in (select tenant_id from tenant_members where user_id = auth.uid())
  ))
  with check (flow_id in (
    select flow_id from flows
    where tenant_id in (select tenant_id from tenant_members where user_id = auth.uid())
  ));

create policy tenant_isolation_flow_edges on flow_edges
  for all
  using (flow_id in (
    select flow_id from flows
    where tenant_id in (select tenant_id from tenant_members where user_id = auth.uid())
  ))
  with check (flow_id in (
    select flow_id from flows
    where tenant_id in (select tenant_id from tenant_members where user_id = auth.uid())
  ));
