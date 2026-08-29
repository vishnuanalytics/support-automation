-- Phase 12 follow-up (missed by 017): the Globex `classify` node carries an
-- explicit `config.model` override still pointing at the retired
-- `llama-3.1-8b-instant` (017 only repointed `type='draft'` nodes; classify
-- normally has no model key and inherits llm.FAST_MODEL, but this one was
-- seeded with an override in 009). Surfaced by tests/test_multiflow.py
-- against the real Groq path: 404 model_not_found.

update flow_nodes
set config = jsonb_set(config, '{model}', '"openai/gpt-oss-20b"')
where type = 'classify'
  and config->>'model' in ('llama-3.3-70b-versatile', 'llama-3.1-8b-instant');

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
       md5(f.flow_id::text || '-020')
from flows f
where f.status = 'published';

update flows f
set published_version = (select max(version) from flow_versions v where v.flow_id = f.flow_id),
    version = version + 1
where f.status = 'published';
