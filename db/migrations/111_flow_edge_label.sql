-- Lets a user name an edge (e.g. "VIP escalation") instead of the canvas
-- only ever showing the raw condition expression as the edge's label.
-- Mirrors flow_nodes.label (001_flow_schema.sql) — same nullable text
-- column shape, same "not part of the routing logic, purely a display
-- name" role. NULL/blank means "no custom name" -> the canvas falls back
-- to showing the condition expression, same as every edge today.

alter table flow_edges add column if not exists label text;

-- replace_flow_graph (012_flow_versions.sql, RLS-wrapped by
-- is_tenant_editor in 032_role_gated_rls.sql — that policy is on the
-- table, not the function body, so it's untouched here) needs the new
-- column in its insert list or a saved edge name is silently dropped on
-- every draft save.
create or replace function replace_flow_graph(
  p_flow_id uuid,
  p_nodes   jsonb,   -- array of {node_id,type,label,position_x,position_y,config}
  p_edges   jsonb    -- array of {edge_id,source_node_id,target_node_id,condition,label}
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

  insert into flow_edges (edge_id, flow_id, source_node_id, target_node_id, condition, label)
  select (e->>'edge_id')::uuid, p_flow_id,
         (e->>'source_node_id')::uuid, (e->>'target_node_id')::uuid,
         coalesce(e->'condition', '{}'::jsonb), e->>'label'
  from jsonb_array_elements(p_edges) e;
end $$;
