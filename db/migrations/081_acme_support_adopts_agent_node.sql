-- Phase 29 step 2 follow-through: Acme/support's `retrieve`+`draft` pair
-- becomes one `agent` node (interpreter/registry.py::h_agent) -- a bounded
-- ReAct loop that reformulates the search query and retries when the
-- first pass's groundedness is low, instead of a single fixed retrieve
-- + draft call. Config reuses both old nodes' settings verbatim (same
-- model, same top_k) -- CLAUDE.md: don't change a seed flow's model
-- without a reason, and this isn't one.
--
-- Topology change: `retrieve -> classify -> sf_writeback -> draft ->
-- confidence_gate -> ...` becomes `classify -> sf_writeback -> agent ->
-- confidence_gate -> ...`. `retrieve` doesn't actually depend on
-- `classify`'s output (both only read the case text), so dropping it as
-- the entry node and starting from `classify` instead is a safe
-- reordering, not a behavior change -- `agent` still needs `classify` to
-- have already run before it drafts (h_draft reads state.classification
-- for the diagnostic-answer_mode grounding rule).
--
-- Applied live to this project by hand (a script mirroring
-- api.main.publish_flow's exact logic: validate the draft with the real
-- check_flow, snapshot flow_versions with the real definition_hash, bump
-- the flows row) -- this migration makes the same end state reproducible
-- on a fresh environment. Idempotent: guarded on the old `retrieve` node
-- still being present, so re-running (or running where 003/008/009 were
-- freshly (re-)applied) is a no-op once done. Verified: test_multiflow.py
-- 4/4 live against the real published flow. Fully reversible regardless
-- -- the pre-change flow_versions row is untouched, so rollback_flow
-- restores the old retrieve+draft pair instantly.

do $$
declare
  v_flow_id uuid := '11111111-1111-1111-1111-111111111111';
  v_retrieve_id uuid := '61ac57b5-34b3-5097-afd4-e9cdbf00b245';
  v_draft_id uuid := '2e18fa56-a635-530b-a5be-7b91eb6ba683';
  v_agent_id uuid := 'd0e1a9e2-0000-4000-8000-000000000001';
  v_sfwb_id uuid := '3b9a1f2c-5d6e-4f70-8a1b-000000000008';
  v_gate_id uuid := '7f6c96dc-273f-55cf-9d1f-5519045c839c';
  v_version int;
  v_flow_version int;
  v_hash text;
begin
  -- already migrated (or a fresh env whose seed predates 003/008/009
  -- re-application) -- nothing to do.
  if not exists (select 1 from flow_nodes where node_id = v_retrieve_id and flow_id = v_flow_id) then
    return;
  end if;

  insert into flow_nodes (flow_id, node_id, type, label, position_x, position_y, config)
  values (
    v_flow_id, v_agent_id, 'agent', 'Retrieve + draft (agent)', 250, 250,
    jsonb_build_object(
      'retrieve', jsonb_build_object('top_k', 5, 'source', jsonb_build_array('supabase', 'neo4j')),
      'draft', jsonb_build_object('model', 'llama-3.3-70b-versatile', 'max_tokens', 500),
      'max_iterations', 3,
      'groundedness_threshold', 0.6
    )
  );

  insert into flow_edges (flow_id, source_node_id, target_node_id, condition)
  values
    (v_flow_id, v_sfwb_id, v_agent_id, '{}'::jsonb),
    (v_flow_id, v_agent_id, v_gate_id, '{}'::jsonb);

  delete from flow_edges where flow_id = v_flow_id and source_node_id = v_retrieve_id;
  delete from flow_edges where flow_id = v_flow_id and source_node_id = v_sfwb_id and target_node_id = v_draft_id;
  delete from flow_edges where flow_id = v_flow_id and source_node_id = v_draft_id;

  delete from flow_nodes where flow_id = v_flow_id and node_id = v_retrieve_id;
  delete from flow_nodes where flow_id = v_flow_id and node_id = v_draft_id;

  select coalesce(max(version), 0) + 1 into v_version from flow_versions where flow_id = v_flow_id;
  v_hash := md5(v_flow_id::text || '-081-agent-adoption');

  if not exists (select 1 from flow_versions where flow_id = v_flow_id and definition_hash = v_hash) then
    insert into flow_versions (flow_id, version, name, nodes, edges, definition_hash)
    select f.flow_id, v_version, f.name,
           (select jsonb_agg(jsonb_build_object('node_id', n.node_id, 'type', n.type, 'label', n.label,
                   'position_x', n.position_x, 'position_y', n.position_y, 'config', n.config))
            from flow_nodes n where n.flow_id = f.flow_id),
           (select jsonb_agg(jsonb_build_object('edge_id', e.edge_id, 'source_node_id', e.source_node_id,
                   'target_node_id', e.target_node_id, 'condition', e.condition))
            from flow_edges e where e.flow_id = f.flow_id),
           v_hash
    from flows f where f.flow_id = v_flow_id;

    select version into v_flow_version from flows where flow_id = v_flow_id;
    update flows set status = 'published', published_version = v_version, version = v_flow_version + 1
    where flow_id = v_flow_id;
  end if;
end $$;
