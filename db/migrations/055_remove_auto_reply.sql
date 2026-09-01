-- Phase 24a: no case gets an emailed AI response from automation.
--
-- The only path that auto-sent a customer email was `auto_reply` (support +
-- confidence_gate PASS). Remove the node and route that case to
-- `notify_human` instead — like every other path. From here every response
-- goes through a human (Phase 24b/c add the Slack reasoning dialogue;
-- `notify` / `clarify` stay as interim draft-for-review nodes and are folded
-- into `notify_human` in 24c).
--
-- Email flow (e5e5e5e5…). Publishes the next version.

delete from flow_edges
 where flow_id = 'e5e5e5e5-5555-4555-8555-555555555555'
   and source_node_id = 'e5000007-5555-4555-8555-555555555555'   -- confidence_gate
   and target_node_id = 'e5000008-5555-4555-8555-555555555555';  -- auto_reply

delete from flow_nodes
 where node_id = 'e5000008-5555-4555-8555-555555555555';          -- auto_reply

insert into flow_edges (edge_id, flow_id, source_node_id, target_node_id, condition) values
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000007-5555-4555-8555-555555555555', 'e5000013-5555-4555-8555-555555555555',
  '{"if": "confidence_gate.pass and tier != ''enterprise'' and routed_team == ''support''"}'::jsonb);

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
    md5(fid::text || '-055')
  from flows f where f.flow_id = fid
  on conflict (flow_id, version) do nothing;
  update flows set version = nextv, published_version = nextv where flow_id = fid;
end $$;
