-- Phase 24e: the notify_human node now drives the Slack reasoning dialogue
-- (LLM-pruned questions asked in one go, ≤ max_rounds follow-ups, then draft +
-- approve). Surface `max_rounds` on the seeded node and relabel it.
--
-- Email flow (e5e5e5e5…). Publishes the next version.

update flow_nodes
   set config = config || '{"max_rounds": 3}'::jsonb,
       label  = 'Reason with a human in Slack, then reply'
 where node_id = 'e5000013-5555-4555-8555-555555555555';

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
    md5(fid::text || '-058')
  from flows f where f.flow_id = fid
  on conflict (flow_id, version) do nothing;
  update flows set version = nextv, published_version = nextv where flow_id = fid;
end $$;
