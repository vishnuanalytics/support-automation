-- Phase 21: splice `case_lookup` into the email sf_entry flow, between
-- sf_writeback and draft — so `draft` answers from past resolutions
-- (case_memory + Neo4j) as well as KB docs. Best-effort: no memory rows / no
-- embedder -> the node is a no-op and draft is unchanged.
--
--   … sf_writeback → case_lookup → draft → confidence_gate …
--
-- Publishes the next version. Portable copy: flow_email_l0l1.json.

insert into flow_nodes (node_id, flow_id, type, label, position_x, position_y, config) values
 ('e5000012-5555-4555-8555-555555555555', 'e5e5e5e5-5555-4555-8555-555555555555',
  'case_lookup', 'Recall similar resolved Cases (Phase 21)', 900, 40,
  '{"k": 3, "pool": 10, "min_similarity": 0.35, "min_memories": 3, "use_graph": true, "skip_modes": ["action"]}'::jsonb)
on conflict (node_id) do nothing;

delete from flow_edges
 where flow_id = 'e5e5e5e5-5555-4555-8555-555555555555'
   and source_node_id = 'e5000005-5555-4555-8555-555555555555'
   and target_node_id = 'e5000006-5555-4555-8555-555555555555';
insert into flow_edges (edge_id, flow_id, source_node_id, target_node_id, condition) values
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000005-5555-4555-8555-555555555555', 'e5000012-5555-4555-8555-555555555555', '{}'::jsonb),
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000012-5555-4555-8555-555555555555', 'e5000006-5555-4555-8555-555555555555', '{}'::jsonb);

-- publish v(next)
do $$
declare
  fid uuid := 'e5e5e5e5-5555-4555-8555-555555555555';
  nextv int;
begin
  select coalesce(published_version, version, 1) + 1 into nextv from flows where flow_id = fid;
  insert into flow_versions (flow_id, version, name, nodes, edges, definition_hash)
  select fid, nextv, f.name,
    (select jsonb_agg(jsonb_build_object('node_id',n.node_id,'type',n.type,'label',n.label,
            'position_x',n.position_x,'position_y',n.position_y,'config',n.config))
     from flow_nodes n where n.flow_id = fid),
    (select jsonb_agg(jsonb_build_object('edge_id',e.edge_id,'source_node_id',e.source_node_id,
            'target_node_id',e.target_node_id,'condition',e.condition))
     from flow_edges e where e.flow_id = fid),
    md5(fid::text || '-049')
  from flows f where f.flow_id = fid
  on conflict (flow_id, version) do nothing;

  update flows set version = nextv, published_version = nextv where flow_id = fid;
end $$;
