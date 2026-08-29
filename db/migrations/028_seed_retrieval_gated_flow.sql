-- Seed: "Support — retrieval-gated triage" (team support-triage, Acme).
--
-- Distinct from the L0/L1 flow in that it gates on the CONFIDENCE OF THE
-- RETRIEVED CONTEXT *before* drafting — a `confidence_gate` weighted
-- {retrieval:1, draft:0, groundedness:0} right after classify, so its
-- score == retrieval_score. Only cases the docs actually cover get a
-- drafted reply; thin-retrieval cases go straight to a human.
--
--   retrieve -> classify -> retrieval_gate
--     retrieval_gate --tier==enterprise-->                 handover
--     retrieval_gate --pass & not enterprise--> draft -> answer_gate
--                                                 answer_gate --pass--> auto_reply
--                                                 answer_gate --fail--> ask_human
--     retrieval_gate --fail & not enterprise-->            ask_human
--
-- Portable copy: interpreter/flows/flow_retrieval_gated.json

insert into flows (flow_id, tenant_id, team, name, status, version, published_version)
values ('d4d4d4d4-4444-4444-8444-444444444444',
        '00000000-0000-0000-0000-000000000000',
        'support-triage', 'Support — retrieval-gated triage', 'published', 1, 1)
on conflict (flow_id) do nothing;

insert into flow_nodes (node_id, flow_id, type, label, position_x, position_y, config) values
 ('d4000001-4444-4444-8444-444444444444', 'd4d4d4d4-4444-4444-8444-444444444444',
  'retrieve', 'Retrieve KB context', 0, 100,
  '{"source": ["supabase"], "top_k": 5, "use_rerank": true}'::jsonb),
 ('d4000002-4444-4444-8444-444444444444', 'd4d4d4d4-4444-4444-8444-444444444444',
  'classify', 'Classify tier / topic / urgency', 200, 100,
  '{"tier_field": "account.customer_type", "region_field": "account.region"}'::jsonb),
 ('d4000003-4444-4444-8444-444444444444', 'd4d4d4d4-4444-4444-8444-444444444444',
  'confidence_gate', 'Gate on retrieval confidence', 400, 100,
  '{"weights": {"retrieval": 1.0, "draft": 0.0, "groundedness": 0.0},
    "default_threshold": 0.45,
    "tier_overrides": {"basic": 0.40, "premium": 0.50, "enterprise": 0.60},
    "escalate_topics": ["billing", "refund", "pricing", "compliance", "legal",
                        "account-access", "data-export", "cancellation"]}'::jsonb),
 ('d4000004-4444-4444-8444-444444444444', 'd4d4d4d4-4444-4444-8444-444444444444',
  'draft', 'Draft reply from context', 600, 40,
  '{"model": "openai/gpt-oss-120b", "max_tokens": 900}'::jsonb),
 ('d4000005-4444-4444-8444-444444444444', 'd4d4d4d4-4444-4444-8444-444444444444',
  'confidence_gate', 'Gate on answer quality', 800, 40,
  '{"weights": {"retrieval": 0.4, "draft": 0.1, "groundedness": 0.5},
    "default_threshold": 0.6,
    "tier_overrides": {"basic": 0.55, "premium": 0.65, "enterprise": 0.75}}'::jsonb),
 ('d4000006-4444-4444-8444-444444444444', 'd4d4d4d4-4444-4444-8444-444444444444',
  'auto_reply', 'Send the drafted reply', 1000, 0, '{}'::jsonb),
 ('d4000007-4444-4444-8444-444444444444', 'd4d4d4d4-4444-4444-8444-444444444444',
  'ask_human', 'Route to a support agent', 1000, 160,
  '{"channel": "salesforce_chatter", "reason": "docs_thin_or_answer_unsure"}'::jsonb),
 ('d4000008-4444-4444-8444-444444444444', 'd4d4d4d4-4444-4444-8444-444444444444',
  'handover', 'Full handover (enterprise)', 600, 220,
  '{"reason": "enterprise_tier"}'::jsonb)
on conflict (node_id) do nothing;

insert into flow_edges (edge_id, flow_id, source_node_id, target_node_id, condition) values
 (gen_random_uuid(), 'd4d4d4d4-4444-4444-8444-444444444444',
  'd4000001-4444-4444-8444-444444444444', 'd4000002-4444-4444-8444-444444444444', '{}'::jsonb),
 (gen_random_uuid(), 'd4d4d4d4-4444-4444-8444-444444444444',
  'd4000002-4444-4444-8444-444444444444', 'd4000003-4444-4444-8444-444444444444', '{}'::jsonb),
 (gen_random_uuid(), 'd4d4d4d4-4444-4444-8444-444444444444',
  'd4000003-4444-4444-8444-444444444444', 'd4000008-4444-4444-8444-444444444444',
  '{"if": "tier == ''enterprise''"}'::jsonb),
 (gen_random_uuid(), 'd4d4d4d4-4444-4444-8444-444444444444',
  'd4000003-4444-4444-8444-444444444444', 'd4000004-4444-4444-8444-444444444444',
  '{"if": "confidence_gate.pass and tier != ''enterprise''"}'::jsonb),
 (gen_random_uuid(), 'd4d4d4d4-4444-4444-8444-444444444444',
  'd4000003-4444-4444-8444-444444444444', 'd4000007-4444-4444-8444-444444444444',
  '{"if": "not confidence_gate.pass and tier != ''enterprise''"}'::jsonb),
 (gen_random_uuid(), 'd4d4d4d4-4444-4444-8444-444444444444',
  'd4000004-4444-4444-8444-444444444444', 'd4000005-4444-4444-8444-444444444444', '{}'::jsonb),
 (gen_random_uuid(), 'd4d4d4d4-4444-4444-8444-444444444444',
  'd4000005-4444-4444-8444-444444444444', 'd4000006-4444-4444-8444-444444444444',
  '{"if": "confidence_gate.pass"}'::jsonb),
 (gen_random_uuid(), 'd4d4d4d4-4444-4444-8444-444444444444',
  'd4000005-4444-4444-8444-444444444444', 'd4000007-4444-4444-8444-444444444444',
  '{"if": "not confidence_gate.pass"}'::jsonb)
on conflict (edge_id) do nothing;

-- v1 snapshot so `status='published'` runs load from flow_versions
insert into flow_versions (flow_id, version, name, nodes, edges, definition_hash)
select 'd4d4d4d4-4444-4444-8444-444444444444', 1, f.name,
       (select jsonb_agg(jsonb_build_object('node_id', n.node_id, 'type', n.type, 'label', n.label,
               'position_x', n.position_x, 'position_y', n.position_y, 'config', n.config))
        from flow_nodes n where n.flow_id = f.flow_id),
       (select jsonb_agg(jsonb_build_object('edge_id', e.edge_id, 'source_node_id', e.source_node_id,
               'target_node_id', e.target_node_id, 'condition', e.condition))
        from flow_edges e where e.flow_id = f.flow_id),
       md5('d4d4d4d4-4444-4444-8444-444444444444-028')
from flows f where f.flow_id = 'd4d4d4d4-4444-4444-8444-444444444444'
on conflict (flow_id, version) do nothing;
