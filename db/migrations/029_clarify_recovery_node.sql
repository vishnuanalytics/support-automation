-- Phase 17a: low-confidence recovery — the `clarify` node.
--
-- On the "Support — retrieval-gated triage" flow (d4d4…, team support-triage,
-- Acme), a thin-retrieval FAIL used to route straight to `ask_human`. It now
-- splits:
--   * FAIL + a forced-escalation topic (billing / legal / …)  -> ask_human
--       (unchanged — those are never a docs answer)
--   * FAIL + a benign topic                                   -> clarify
--       (NEW — ask the customer the specific missing details; their reply
--        arrives as a new case and can clear the gate next round)
--
-- The four retrieval_gate outgoing conditions stay mutually exclusive, so
-- routing does not depend on edge order. `clarify` is terminal.
--
-- No code change needed for the node type itself (`type` is a free string;
-- the handler is `interpreter/registry.h_clarify`). Portable copy:
-- interpreter/flows/flow_retrieval_gated.json.

-- ---- the new node -----------------------------------------------------------
insert into flow_nodes (node_id, flow_id, type, label, position_x, position_y, config) values
 ('d4000009-4444-4444-8444-444444444444', 'd4d4d4d4-4444-4444-8444-444444444444',
  'clarify', 'Ask the customer for missing details', 1000, 300,
  '{"max_questions": 3, "auto_send": false, "channel": "email"}'::jsonb)
on conflict (node_id) do nothing;

-- ---- re-route the retrieval_gate FAIL branch ------------------------------
-- drop the old catch-all FAIL edge (retrieval_gate -> ask_human)
delete from flow_edges
 where flow_id       = 'd4d4d4d4-4444-4444-8444-444444444444'
   and source_node_id = 'd4000003-4444-4444-8444-444444444444'
   and target_node_id = 'd4000007-4444-4444-8444-444444444444'
   and coalesce(condition->>'if', '') = 'not confidence_gate.pass and tier != ''enterprise''';

-- add: forced-escalation FAIL -> ask_human ; benign FAIL -> clarify
-- (guarded by NOT EXISTS so re-running the migration is a no-op)
insert into flow_edges (edge_id, flow_id, source_node_id, target_node_id, condition)
select gen_random_uuid(),
       'd4d4d4d4-4444-4444-8444-444444444444'::uuid,
       'd4000003-4444-4444-8444-444444444444'::uuid,
       v.tgt::uuid,
       v.cond::jsonb
from (values
  ('d4000007-4444-4444-8444-444444444444',
   '{"if": "not confidence_gate.pass and tier != ''enterprise'' and confidence_gate.forced_escalation"}'),
  ('d4000009-4444-4444-8444-444444444444',
   '{"if": "not confidence_gate.pass and tier != ''enterprise'' and not confidence_gate.forced_escalation"}')
) as v(tgt, cond)
where not exists (
  select 1 from flow_edges e
   where e.flow_id        = 'd4d4d4d4-4444-4444-8444-444444444444'
     and e.source_node_id = 'd4000003-4444-4444-8444-444444444444'
     and e.target_node_id = v.tgt::uuid
);

-- ---- re-snapshot this flow as the next published version -----------------
insert into flow_versions (flow_id, version, name, nodes, edges, definition_hash)
select 'd4d4d4d4-4444-4444-8444-444444444444',
       coalesce((select max(version) from flow_versions v
                 where v.flow_id = 'd4d4d4d4-4444-4444-8444-444444444444'), 0) + 1,
       f.name,
       (select jsonb_agg(jsonb_build_object('node_id', n.node_id, 'type', n.type, 'label', n.label,
               'position_x', n.position_x, 'position_y', n.position_y, 'config', n.config))
        from flow_nodes n where n.flow_id = f.flow_id),
       (select jsonb_agg(jsonb_build_object('edge_id', e.edge_id, 'source_node_id', e.source_node_id,
               'target_node_id', e.target_node_id, 'condition', e.condition))
        from flow_edges e where e.flow_id = f.flow_id),
       md5('d4d4d4d4-4444-4444-8444-444444444444-029')
from flows f
where f.flow_id = 'd4d4d4d4-4444-4444-8444-444444444444'
  and not exists (
    select 1 from flow_versions v
     where v.flow_id = 'd4d4d4d4-4444-4444-8444-444444444444'
       and v.definition_hash = md5('d4d4d4d4-4444-4444-8444-444444444444-029')
  );

update flows
   set published_version = (select max(version) from flow_versions v
                            where v.flow_id = flows.flow_id),
       version = version + 1
 where flow_id = 'd4d4d4d4-4444-4444-8444-444444444444'
   and published_version < (select max(version) from flow_versions v
                            where v.flow_id = flows.flow_id);
