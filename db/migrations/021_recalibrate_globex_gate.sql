-- Phase 7 recalibration, part 2: the Globex "human-review-first" flow.
-- Its gate was threshold 0.9 / {basic .9, premium .95, enterprise .99} on
-- the legacy 0.5/0.5 blend — tuned against the LLM stub. With a real Groq
-- draft (draft_confidence ~0.95) a well-covered how-to now scores ~0.93 and
-- auto-sends, contradicting the flow's whole premise (surfaced by
-- tests/test_multiflow.py).
--
-- "Human-review-first" = non-enterprise tiers ALWAYS go to a human. Make
-- that explicit: basic/premium thresholds are set above 1.0 (a blended
-- score can't reach them), so only enterprise can auto-send, and only on a
-- near-perfect retrieval+groundedness signal (draft self-confidence
-- weighted to 0 here).

update flow_nodes
set config = jsonb_build_object(
  'default_threshold', 0.95,
  'tier_overrides', jsonb_build_object('basic', 1.01, 'premium', 1.01, 'enterprise', 0.95),
  'weights', jsonb_build_object('retrieval', 0.7, 'draft', 0.0, 'groundedness', 0.3)
)
where flow_id = 'a2a2a2a2-2222-4222-8222-222222222222'
  and type = 'confidence_gate';

-- re-snapshot every published flow so runs pick up the change
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
       md5(f.flow_id::text || '-021')
from flows f
where f.status = 'published';

update flows f
set published_version = (select max(version) from flow_versions v where v.flow_id = f.flow_id),
    version = version + 1
where f.status = 'published';
