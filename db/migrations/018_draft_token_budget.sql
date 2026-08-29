-- Phase 12 follow-up: give the `draft` nodes enough completion budget.
-- The Groq gpt-oss models spend part of `max_tokens` on hidden reasoning
-- tokens (even at reasoning_effort=low), so a 500-token cap truncated the
-- JSON reply mid-string and Groq's server-side JSON grammar 400'd the call
-- (`json_validate_failed`). interpreter.llm now retries such a failure
-- free-form, but the fix belongs in the config: bump draft budgets so the
-- first attempt succeeds.

update flow_nodes
set config = jsonb_set(config, '{max_tokens}', '900')
where type = 'draft'
  and coalesce((config->>'max_tokens')::int, 0) < 900;

-- re-snapshot every published flow so runs pick up the new budget
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
       md5(f.flow_id::text || '-018')
from flows f
where f.status = 'published';

update flows f
set published_version = (select max(version) from flow_versions v where v.flow_id = f.flow_id),
    version = version + 1
where f.status = 'published';
