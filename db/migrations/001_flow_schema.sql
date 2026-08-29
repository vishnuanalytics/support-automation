-- Phase 0: flow-definition schema
-- Generic node types (config-driven), per-node/per-tier confidence,
-- multi-tenant + multi-flow from day 1.

create extension if not exists pgcrypto;

create table if not exists flows (
  flow_id      uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null,
  team         text not null,                    -- 'support' | 'csm' | 'sales' | 'offboarding'
  name         text not null,
  version      int  not null default 1,
  status       text not null default 'draft',    -- 'draft' | 'published' | 'archived'
  created_at   timestamptz default now(),
  updated_at   timestamptz default now()
);

create table if not exists flow_nodes (
  node_id      uuid primary key default gen_random_uuid(),
  flow_id      uuid not null references flows(flow_id) on delete cascade,
  type         text not null,                    -- free string, no fixed enum
  label        text,
  position_x   int,
  position_y   int,
  config       jsonb not null default '{}'::jsonb
);

create table if not exists flow_edges (
  edge_id        uuid primary key default gen_random_uuid(),
  flow_id        uuid not null references flows(flow_id) on delete cascade,
  source_node_id uuid not null references flow_nodes(node_id) on delete cascade,
  target_node_id uuid not null references flow_nodes(node_id) on delete cascade,
  condition      jsonb not null default '{}'::jsonb
);

create index if not exists idx_flows_tenant on flows(tenant_id);
create index if not exists idx_flows_tenant_team_status on flows(tenant_id, team, status);
create index if not exists idx_flow_nodes_flow on flow_nodes(flow_id);
create index if not exists idx_flow_edges_flow on flow_edges(flow_id);
create index if not exists idx_flow_edges_source on flow_edges(source_node_id);
create index if not exists idx_flow_edges_target on flow_edges(target_node_id);
