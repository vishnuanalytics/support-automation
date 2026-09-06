-- Follow-up to running eval/agent/run_agent_eval.py for real this session
-- (PROJECT_SCOPE.md): every call from Acme/support's live `agent` node
-- 404'd on Groq before falling back to a free OpenRouter model, silently,
-- every single time.
--
-- Root cause: migration 081 built the `agent` node's nested
-- `config.draft.model` from a hardcoded literal ('llama-3.3-70b-versatile')
-- instead of actually copying the sibling `draft` node's live value at the
-- time -- despite 081's own comment claiming "reuses both old nodes'
-- settings verbatim". That old `draft` node's `config.model` had already
-- been repointed by migration 017 (Phase 12, "Groq retired the llama-3.x
-- model names") four years of migration history earlier; 081 silently
-- reintroduced the exact string 017 fixed, just one level deeper in the
-- jsonb (top-level `config.model` on a `draft`-type node, which 017's
-- `where type = 'draft'` matched; nested `config.draft.model` on an
-- `agent`-type node, which it didn't). Same bug class 020 already hit once
-- for `classify` nodes -- same fix shape here.

update flow_nodes
set config = jsonb_set(config, '{draft,model}', '"openai/gpt-oss-120b"')
where type = 'agent'
  and config #>> '{draft,model}' in ('llama-3.3-70b-versatile', 'llama-3.1-8b-instant');

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
       md5(f.flow_id::text || '-088')
from flows f
where f.status = 'published';

update flows f
set published_version = (select max(version) from flow_versions v where v.flow_id = f.flow_id),
    version = version + 1
where f.status = 'published';
