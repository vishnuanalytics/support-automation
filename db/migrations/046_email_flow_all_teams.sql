-- Phase 20p: make the email L0/L1 flow (e5e5e5e5…, the sf_entry flow) the
-- single comprehensive workflow — every team, every scenario.
--
-- v3 had no team routing: csm / sales / offboarding cases fell through to
-- `notify` or `clarify` like any support case. v4 inserts `team_route` and
-- widens the confidence_gate to a 5-way split:
--
--   enterprise tier OR routed_team == offboarding  -> handover
--       (Enterprise_Support / Team_Offboarding — resolved by h_handover)
--   routed_team in (csm, sales)  [non-enterprise]  -> ask_human  (Team_CSM / Team_Sales)
--   gate PASS  [support team]                      -> auto_reply
--   gate FAIL + forced escalation  [support]       -> notify   (Case.Type -> notify_targets; Case stays put)
--   gate FAIL + not forced         [support]       -> clarify  (ask the customer; 2 rounds -> Team_Support)
--
-- team_route keyword rules -> routed_team in {support, csm, sales, offboarding};
-- billing / login / bug stay `support` and are handled by the gate's forced
-- escalation -> notify (routed by Case.Type). Publishes v4.
--
-- Portable copy: interpreter/flows/flow_email_l0l1.json.

-- 1. new nodes: team_route + ask_human (ask_human was dropped in 20n / migration 044)
insert into flow_nodes (node_id, flow_id, type, label, position_x, position_y, config) values
 ('e5000010-5555-4555-8555-555555555555', 'e5e5e5e5-5555-4555-8555-555555555555',
  'team_route', 'Route to a team (support / csm / sales / offboarding)', 700, 100,
  '{"default": "support"}'::jsonb),
 ('e5000011-5555-4555-8555-555555555555', 'e5e5e5e5-5555-4555-8555-555555555555',
  'ask_human', 'Escalate to the owning team (csm / sales) — reassigns the Case', 1400, 340,
  '{"channel": "salesforce_chatter", "queue_by_team": {"csm": "Team_CSM", "sales": "Team_Sales"}, "escalate_queue": "Billing_Escalations"}'::jsonb)
on conflict (node_id) do nothing;

-- 2. handover: enterprise + offboarding
update flow_nodes
   set label  = 'Full handover (enterprise / offboarding)',
       config = jsonb_build_object(
         'reason', 'enterprise_or_offboarding',
         'enterprise_queue', 'Enterprise_Support',
         'queue_by_team', jsonb_build_object('offboarding', 'Team_Offboarding'))
 where node_id = 'e500000a-5555-4555-8555-555555555555';

-- 3. splice team_route between classify and sf_writeback
delete from flow_edges
 where flow_id = 'e5e5e5e5-5555-4555-8555-555555555555'
   and source_node_id = 'e5000004-5555-4555-8555-555555555555'
   and target_node_id = 'e5000005-5555-4555-8555-555555555555';
insert into flow_edges (edge_id, flow_id, source_node_id, target_node_id, condition) values
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000004-5555-4555-8555-555555555555', 'e5000010-5555-4555-8555-555555555555', '{}'::jsonb),
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000010-5555-4555-8555-555555555555', 'e5000005-5555-4555-8555-555555555555', '{}'::jsonb);

-- 4. rebuild the confidence_gate outgoing edges (5-way, mutually exclusive)
delete from flow_edges
 where flow_id = 'e5e5e5e5-5555-4555-8555-555555555555'
   and source_node_id = 'e5000007-5555-4555-8555-555555555555';
insert into flow_edges (edge_id, flow_id, source_node_id, target_node_id, condition) values
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000007-5555-4555-8555-555555555555', 'e500000a-5555-4555-8555-555555555555',
  '{"if": "tier == ''enterprise'' or routed_team == ''offboarding''"}'::jsonb),
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000007-5555-4555-8555-555555555555', 'e5000011-5555-4555-8555-555555555555',
  '{"if": "routed_team in (''csm'', ''sales'') and tier != ''enterprise''"}'::jsonb),
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000007-5555-4555-8555-555555555555', 'e5000008-5555-4555-8555-555555555555',
  '{"if": "confidence_gate.pass and tier != ''enterprise'' and routed_team == ''support''"}'::jsonb),
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000007-5555-4555-8555-555555555555', 'e500000b-5555-4555-8555-555555555555',
  '{"if": "not confidence_gate.pass and tier != ''enterprise'' and routed_team == ''support'' and confidence_gate.forced_escalation"}'::jsonb),
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000007-5555-4555-8555-555555555555', 'e500000c-5555-4555-8555-555555555555',
  '{"if": "not confidence_gate.pass and tier != ''enterprise'' and routed_team == ''support'' and not confidence_gate.forced_escalation"}'::jsonb);

-- 5. publish v4
update flows set version = 4, published_version = 4
 where flow_id = 'e5e5e5e5-5555-4555-8555-555555555555';
insert into flow_versions (flow_id, version, name, nodes, edges, definition_hash)
select 'e5e5e5e5-5555-4555-8555-555555555555', 4, f.name,
       (select jsonb_agg(jsonb_build_object('node_id', n.node_id, 'type', n.type, 'label', n.label,
               'position_x', n.position_x, 'position_y', n.position_y, 'config', n.config))
        from flow_nodes n where n.flow_id = f.flow_id),
       (select jsonb_agg(jsonb_build_object('edge_id', e.edge_id, 'source_node_id', e.source_node_id,
               'target_node_id', e.target_node_id, 'condition', e.condition))
        from flow_edges e where e.flow_id = f.flow_id),
       md5('e5e5e5e5-5555-4555-8555-555555555555-046')
from flows f where f.flow_id = 'e5e5e5e5-5555-4555-8555-555555555555'
on conflict (flow_id, version) do nothing;
