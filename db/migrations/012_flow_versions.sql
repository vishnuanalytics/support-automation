-- Phase 8: immutable flow versions + a transactional save.
--
-- flow_nodes / flow_edges stay the *editable working draft* (one per flow —
-- what the editor mutates). Publishing snapshots that draft into an
-- immutable flow_versions row; runs execute a *published snapshot*, not the
-- live draft, and record which version they ran. Rollback re-points the
-- published pointer and restores the draft from that snapshot.

create table if not exists flow_versions (
  version_id      uuid primary key default gen_random_uuid(),
  flow_id         uuid not null references flows(flow_id) on delete cascade,
  version         int  not null,
  name            text not null,
  nodes           jsonb not null,          -- [{node_id,type,label,position_x,position_y,config}]
  edges           jsonb not null,          -- [{edge_id,source_node_id,target_node_id,condition}]
  definition_hash text not null,           -- sha256 of the canonical nodes+edges json
  created_by      uuid,                    -- auth.uid() at publish time
  created_at      timestamptz not null default now(),
  unique (flow_id, version)
);
create index if not exists idx_flow_versions_flow on flow_versions (flow_id, version desc);

alter table flows add column if not exists published_version int;   -- -> flow_versions.version
alter table runs  add column if not exists flow_version int;        -- which version executed

-- RLS: mirror the flow tables (002)
alter table flow_versions enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'tenant_isolation_flow_versions'
                 and polrelid = 'public.flow_versions'::regclass) then
    create policy tenant_isolation_flow_versions on flow_versions
      for all
      using (flow_id in (select flow_id from flows
             where tenant_id in (select tenant_id from tenant_members where user_id = auth.uid())))
      with check (flow_id in (select flow_id from flows
             where tenant_id in (select tenant_id from tenant_members where user_id = auth.uid())));
  end if;
end $$;

-- ---- transactional draft save -------------------------------------------
-- Replace a flow's working draft (flow_nodes + flow_edges) atomically.
-- SECURITY INVOKER so the caller's RLS still applies.
create or replace function replace_flow_graph(
  p_flow_id uuid,
  p_nodes   jsonb,   -- array of {node_id,type,label,position_x,position_y,config}
  p_edges   jsonb    -- array of {edge_id,source_node_id,target_node_id,condition}
)
returns void
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
begin
  delete from flow_edges where flow_id = p_flow_id;
  delete from flow_nodes where flow_id = p_flow_id;

  insert into flow_nodes (node_id, flow_id, type, label, position_x, position_y, config)
  select (n->>'node_id')::uuid, p_flow_id, n->>'type', n->>'label',
         nullif(n->>'position_x','')::int, nullif(n->>'position_y','')::int,
         coalesce(n->'config', '{}'::jsonb)
  from jsonb_array_elements(p_nodes) n;

  insert into flow_edges (edge_id, flow_id, source_node_id, target_node_id, condition)
  select (e->>'edge_id')::uuid, p_flow_id,
         (e->>'source_node_id')::uuid, (e->>'target_node_id')::uuid,
         coalesce(e->'condition', '{}'::jsonb)
  from jsonb_array_elements(p_edges) e;
end $$;

-- ---- backfill v1 for the flows that are already published --------------
insert into flow_versions (flow_id, version, name, nodes, edges, definition_hash, created_at)
select f.flow_id, 1, f.name,
  coalesce((select jsonb_agg(jsonb_build_object(
      'node_id', n.node_id, 'type', n.type, 'label', n.label,
      'position_x', n.position_x, 'position_y', n.position_y, 'config', n.config))
    from flow_nodes n where n.flow_id = f.flow_id), '[]'::jsonb),
  coalesce((select jsonb_agg(jsonb_build_object(
      'edge_id', e.edge_id, 'source_node_id', e.source_node_id,
      'target_node_id', e.target_node_id, 'condition', e.condition))
    from flow_edges e where e.flow_id = f.flow_id), '[]'::jsonb),
  md5(f.flow_id::text || '-v1-backfill'),   -- placeholder; app recomputes on next publish
  now()
from flows f
where f.status = 'published'
  and not exists (select 1 from flow_versions v where v.flow_id = f.flow_id);

update flows set published_version = 1
where status = 'published' and published_version is null;
