-- Phase 17b: sender identification — the `identify` node.
--
-- On "Support — retrieval-gated triage" (d4d4…), insert an `identify` step
-- between `retrieve` and `classify`:  retrieve -> identify -> classify.
--
-- `identify` resolves the sender against Salesforce — exact Contact/Lead by
-- email, else the email DOMAIN -> an Account (a colleague of an existing
-- customer; free-mail domains skipped), else unknown — and writes
-- `state.sender`. It is pass-through (no routing of its own); the downstream
-- `clarify` node reads `state.sender` and, when the sender is unknown, also
-- asks them to confirm who they are.
--
-- No code change for the node type (`type` is a free string; handler is
-- interpreter/registry.h_identify). Portable copy:
-- interpreter/flows/flow_retrieval_gated.json.

-- ---- the new node -----------------------------------------------------------
insert into flow_nodes (node_id, flow_id, type, label, position_x, position_y, config) values
 ('d400000a-4444-4444-8444-444444444444', 'd4d4d4d4-4444-4444-8444-444444444444',
  'identify', 'Resolve the sender (contact / domain -> account)', 100, 260,
  '{"email_field": "contact.email", "domain_match": true, "create_lead_if_missing": false}'::jsonb)
on conflict (node_id) do nothing;

-- ---- splice it into the chain: retrieve -> identify -> classify ----------
-- re-point the existing retrieve -> classify edge at identify
update flow_edges
   set target_node_id = 'd400000a-4444-4444-8444-444444444444'
 where flow_id        = 'd4d4d4d4-4444-4444-8444-444444444444'
   and source_node_id = 'd4000001-4444-4444-8444-444444444444'
   and target_node_id = 'd4000002-4444-4444-8444-444444444444';

-- add identify -> classify (guarded so re-running is a no-op)
insert into flow_edges (edge_id, flow_id, source_node_id, target_node_id, condition)
select gen_random_uuid(),
       'd4d4d4d4-4444-4444-8444-444444444444'::uuid,
       'd400000a-4444-4444-8444-444444444444'::uuid,
       'd4000002-4444-4444-8444-444444444444'::uuid,
       '{}'::jsonb
where not exists (
  select 1 from flow_edges e
   where e.flow_id        = 'd4d4d4d4-4444-4444-8444-444444444444'
     and e.source_node_id = 'd400000a-4444-4444-8444-444444444444'
     and e.target_node_id = 'd4000002-4444-4444-8444-444444444444'
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
       md5('d4d4d4d4-4444-4444-8444-444444444444-030')
from flows f
where f.flow_id = 'd4d4d4d4-4444-4444-8444-444444444444'
  and not exists (
    select 1 from flow_versions v
     where v.flow_id = 'd4d4d4d4-4444-4444-8444-444444444444'
       and v.definition_hash = md5('d4d4d4d4-4444-4444-8444-444444444444-030')
  );

update flows
   set published_version = (select max(version) from flow_versions v
                            where v.flow_id = flows.flow_id),
       version = version + 1
 where flow_id = 'd4d4d4d4-4444-4444-8444-444444444444'
   and published_version < (select max(version) from flow_versions v
                            where v.flow_id = flows.flow_id);
