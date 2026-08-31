-- Phase 20n: Case.Type triage + the `notify` node.
--
-- Two behaviour changes:
--
--  1. classify now also sets Case.Type; sf_writeback writes it on every pass
--     (field_map default gained "case_type" -> "Type" in interpreter/registry.py,
--     so no per-flow config change is needed for that — the label below just
--     makes it visible in the editor). confidence_gate gains `escalate_types`.
--
--  2. A low-confidence Case no longer always goes to `ask_human` (which
--     reassigns the Case). Instead:
--       * forced escalation (Billing / Account-Login Type, a billing topic, or
--         the Billing & Plans module)  ->  `notify` — a Chatter ping to that
--         Type's internal rep, the Case STAYS in its current queue (no OwnerId
--         change).
--       * not forced, still unclear    ->  `clarify` — ask the customer; after
--         `max_rounds` the Case is handed to Team_Support (owner change).
--
-- Scope note (2026-08-31, project mjohgmivnxfwkqmlojqs): only the email L0/L1
-- flow (e5e5e5e5…) exists in this database and it is the `sf_entry` flow, so
-- this migration touches only that flow. The Case-router flow (f0f0f0f0…,
-- migration 040) was never seeded here — its identical redesign lives in
-- scripts/seed_router_flow.py + interpreter/flows/flow_case_router.json for
-- whenever it is stood up. The email flow already had a v2 snapshot (a node-
-- order cleanup, still ask_human); this publishes v3.
--
-- Portable copy: interpreter/flows/flow_email_l0l1.json.
-- New handler: interpreter/registry.h_notify.

-- 1. gate config: + escalate_modules (was absent), + escalate_types
update flow_nodes
   set config = config
       || '{"escalate_modules": ["Billing & Plans"]}'::jsonb
       || '{"escalate_types": ["Billing", "Account / Login"]}'::jsonb
 where node_id = 'e5000007-5555-4555-8555-555555555555';

-- 2. labels (Type is now part of triage / writeback)
update flow_nodes set label = 'Classify tier / type / topic / urgency'
 where node_id = 'e5000004-5555-4555-8555-555555555555';
update flow_nodes set label = 'Write triage fields to the Case (Type, Module, Priority…)'
 where node_id = 'e5000005-5555-4555-8555-555555555555';

-- 3. drop ask_human (e5000009) and every edge out of the confidence_gate
delete from flow_edges
 where flow_id = 'e5e5e5e5-5555-4555-8555-555555555555'
   and (source_node_id = 'e5000007-5555-4555-8555-555555555555'
        or source_node_id = 'e5000009-5555-4555-8555-555555555555'
        or target_node_id = 'e5000009-5555-4555-8555-555555555555');
delete from flow_nodes where node_id = 'e5000009-5555-4555-8555-555555555555';

-- 4. new nodes: notify + clarify
insert into flow_nodes (node_id, flow_id, type, label, position_x, position_y, config) values
 ('e500000b-5555-4555-8555-555555555555', 'e5e5e5e5-5555-4555-8555-555555555555',
  'notify', 'Ping the Type''s internal rep (Case stays in the queue)', 1400, 140,
  '{"channel": "salesforce_chatter", "target_by_type": {}, "target_by_module": {}, "fallback_target": null}'::jsonb),
 ('e500000c-5555-4555-8555-555555555555', 'e5e5e5e5-5555-4555-8555-555555555555',
  'clarify', 'Ask the customer for missing detail', 1400, 260,
  '{"max_questions": 3, "max_rounds": 2, "auto_send": false, "channel": "email", "handover_queue": "Team_Support"}'::jsonb)
on conflict (node_id) do nothing;

-- 5. new gate edges (mutually exclusive + exhaustive)
insert into flow_edges (edge_id, flow_id, source_node_id, target_node_id, condition) values
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000007-5555-4555-8555-555555555555', 'e500000a-5555-4555-8555-555555555555',
  '{"if": "tier == ''enterprise''"}'::jsonb),
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000007-5555-4555-8555-555555555555', 'e5000008-5555-4555-8555-555555555555',
  '{"if": "confidence_gate.pass and tier != ''enterprise''"}'::jsonb),
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000007-5555-4555-8555-555555555555', 'e500000b-5555-4555-8555-555555555555',
  '{"if": "not confidence_gate.pass and tier != ''enterprise'' and confidence_gate.forced_escalation"}'::jsonb),
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000007-5555-4555-8555-555555555555', 'e500000c-5555-4555-8555-555555555555',
  '{"if": "not confidence_gate.pass and tier != ''enterprise'' and not confidence_gate.forced_escalation"}'::jsonb);

-- 6. publish v3 (v2 already exists — a node-order cleanup that still had ask_human)
update flows set version = 3, published_version = 3
 where flow_id = 'e5e5e5e5-5555-4555-8555-555555555555';
insert into flow_versions (flow_id, version, name, nodes, edges, definition_hash)
select 'e5e5e5e5-5555-4555-8555-555555555555', 3, f.name,
       (select jsonb_agg(jsonb_build_object('node_id', n.node_id, 'type', n.type, 'label', n.label,
               'position_x', n.position_x, 'position_y', n.position_y, 'config', n.config))
        from flow_nodes n where n.flow_id = f.flow_id),
       (select jsonb_agg(jsonb_build_object('edge_id', e.edge_id, 'source_node_id', e.source_node_id,
               'target_node_id', e.target_node_id, 'condition', e.condition))
        from flow_edges e where e.flow_id = f.flow_id),
       md5('e5e5e5e5-5555-4555-8555-555555555555-044')
from flows f where f.flow_id = 'e5e5e5e5-5555-4555-8555-555555555555'
on conflict (flow_id, version) do nothing;
