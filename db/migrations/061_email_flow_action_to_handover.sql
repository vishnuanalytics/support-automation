-- Phase 26 / audit WF-1: an `answer_mode == 'action'` request (cancel account,
-- change plan, export data, onboarding/offboarding) should go straight to a
-- full handover — never `notify` / `clarify` / an auto path. Wire it into the
-- LIVE email flow's gate (it was only in the unused comprehensive template).
--
-- Email flow (e5e5e5e5…). Publishes the next version.

update flow_nodes
   set config = config || '{"escalate_answer_modes": ["action"]}'::jsonb
 where node_id = 'e5000007-5555-4555-8555-555555555555';   -- confidence_gate

-- handover also takes an action request
update flow_edges
   set condition = '{"if": "tier == ''enterprise'' or routed_team == ''offboarding'' or answer_mode == ''action''"}'::jsonb
 where edge_id = '437edd7a-5b87-426c-b348-d273b207929e';

-- the four non-handover branches explicitly exclude action
update flow_edges set condition =
  '{"if": "not confidence_gate.pass and tier != ''enterprise'' and routed_team == ''support'' and not confidence_gate.forced_escalation and answer_mode != ''action''"}'::jsonb
 where edge_id = '13ba9d3a-ffac-4664-8793-53d3271ca4bb';   -- clarify
update flow_edges set condition =
  '{"if": "not confidence_gate.pass and tier != ''enterprise'' and routed_team == ''support'' and confidence_gate.forced_escalation and answer_mode != ''action''"}'::jsonb
 where edge_id = '2a7c7baa-c39f-4aab-9b39-d531971ec612';   -- notify
update flow_edges set condition =
  '{"if": "routed_team in (''csm'', ''sales'') and tier != ''enterprise'' and answer_mode != ''action''"}'::jsonb
 where edge_id = '130f0520-d9d1-4d45-b394-85daf3e4d106';   -- ask_human
update flow_edges set condition =
  '{"if": "confidence_gate.pass and tier != ''enterprise'' and routed_team == ''support'' and answer_mode != ''action''"}'::jsonb
 where edge_id = '76b9b816-486e-4107-b98f-43b636545062';   -- notify_human

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
    md5(fid::text || '-061')
  from flows f where f.flow_id = fid
  on conflict (flow_id, version) do nothing;
  update flows set version = nextv, published_version = nextv where flow_id = fid;
end $$;
