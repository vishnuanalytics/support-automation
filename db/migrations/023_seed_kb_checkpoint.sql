-- Phase 14 seed: a `kb_lookup` checkpoint on the Globex flow.
--
-- Adds one node between `classify` and `sf_writeback`, reached ONLY when
-- classify tags the case as a billing/refund topic. When reached it
-- consults the `globex-billing-runbook` internal collection; `draft` then
-- treats those hits as authoritative. Non-billing cases skip it entirely
-- (the "otherwise ignore it" behaviour).
--
-- The collection's content is populated out-of-band (like the globex-sop
-- source in 016): run
--   python -m scripts.seed_kb_demo
-- which creates the `globex-billing-runbook` collection + one entry and
-- embeds it via ingestion.sources.kb_common.

insert into flow_nodes (node_id, flow_id, type, label, position_x, position_y, config)
values (
  'a2000009-2222-4222-8222-222222222222',
  'a2a2a2a2-2222-4222-8222-222222222222',
  'kb_lookup', 'Check billing runbook', 250, 260,
  '{"collections": ["globex-billing-runbook"], "top_k": 3, "min_score": 0.15,
    "query": "{{case.subject}} {{case.body}}"}'::jsonb
)
on conflict (node_id) do nothing;

-- classify -> kb_lookup, only for billing-ish topics
update flow_edges
set target_node_id = 'a2000009-2222-4222-8222-222222222222',
    condition = '{"if": "classification.topic in (''billing'', ''refund'', ''invoice'', ''pricing'', ''chargeback'')"}'::jsonb
where edge_id = '23685e0a-537a-467d-b099-b2802a8e199b';   -- was classify -> sf_writeback (unconditional)

-- new default: classify -> sf_writeback (the else branch)
insert into flow_edges (edge_id, flow_id, source_node_id, target_node_id, condition)
values (
  gen_random_uuid(), 'a2a2a2a2-2222-4222-8222-222222222222',
  'a2000002-2222-4222-8222-222222222222', 'a2000003-2222-4222-8222-222222222222', '{}'::jsonb
);

-- kb_lookup -> sf_writeback (rejoin the main line)
insert into flow_edges (edge_id, flow_id, source_node_id, target_node_id, condition)
values (
  gen_random_uuid(), 'a2a2a2a2-2222-4222-8222-222222222222',
  'a2000009-2222-4222-8222-222222222222', 'a2000003-2222-4222-8222-222222222222', '{}'::jsonb
);

-- re-snapshot published flows
insert into flow_versions (flow_id, version, name, nodes, edges, definition_hash)
select f.flow_id,
       coalesce((select max(version) from flow_versions v where v.flow_id = f.flow_id), 0) + 1,
       f.name,
       (select jsonb_agg(jsonb_build_object('node_id', n.node_id, 'type', n.type, 'label', n.label,
               'position_x', n.position_x, 'position_y', n.position_y, 'config', n.config))
        from flow_nodes n where n.flow_id = f.flow_id),
       (select jsonb_agg(jsonb_build_object('edge_id', e.edge_id, 'source_node_id', e.source_node_id,
               'target_node_id', e.target_node_id, 'condition', e.condition))
        from flow_edges e where e.flow_id = f.flow_id),
       md5(f.flow_id::text || '-023')
from flows f
where f.status = 'published';

update flows f
set published_version = (select max(version) from flow_versions v where v.flow_id = f.flow_id),
    version = version + 1
where f.status = 'published';
