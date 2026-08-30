-- Phase 20i: the Case-router workflow -- the design doc's single end-to-end flow.
-- Generated from scripts/seed_router_flow.py (the canonical, re-runnable seeder).
-- identify -> sf_case -> retrieve -> classify -> team_route -> sf_writeback ->
-- draft -> confidence_gate -> {auto_reply | ask_human | handover}; the
-- escalation/handover queue is resolved from routed_team (queue_by_team).

insert into flows (flow_id, tenant_id, team, name, status, version, published_version)
values ('f0f0f0f0-0000-4000-8000-000000000000', '00000000-0000-0000-0000-000000000000', 'router', 'Case router — team routing + tag manager', 'published', 1, 1)
on conflict (flow_id) do nothing;

insert into flow_nodes (node_id, flow_id, type, label, config) values
  ('f0000000-0000-4000-8000-000000000001', 'f0f0f0f0-0000-4000-8000-000000000000', 'identify', 'Resolve the sender', '{"email_field": "contact.email", "domain_match": true}'::jsonb),
  ('f0000000-0000-4000-8000-000000000002', 'f0f0f0f0-0000-4000-8000-000000000000', 'sf_case', 'Create / reuse the Salesforce Case', '{"origin": "Web", "status": "New", "reuse": "thread"}'::jsonb),
  ('f0000000-0000-4000-8000-000000000003', 'f0f0f0f0-0000-4000-8000-000000000000', 'retrieve', 'Retrieve KB context', '{"source": ["supabase"], "top_k": 5, "use_rerank": true}'::jsonb),
  ('f0000000-0000-4000-8000-000000000004', 'f0f0f0f0-0000-4000-8000-000000000000', 'classify', 'Classify tier / topic / urgency', '{"tier_field": "account.customer_type", "region_field": "account.region", "default_tier": "basic"}'::jsonb),
  ('f0000000-0000-4000-8000-000000000005', 'f0f0f0f0-0000-4000-8000-000000000000', 'team_route', 'Route to a team (design-doc rules)', '{"default": "support"}'::jsonb),
  ('f0000000-0000-4000-8000-000000000006', 'f0f0f0f0-0000-4000-8000-000000000000', 'sf_writeback', 'Write triage fields to the Case', '{}'::jsonb),
  ('f0000000-0000-4000-8000-000000000007', 'f0f0f0f0-0000-4000-8000-000000000000', 'draft', 'Draft the reply from context', '{"model": "openai/gpt-oss-120b", "max_tokens": 700}'::jsonb),
  ('f0000000-0000-4000-8000-000000000008', 'f0f0f0f0-0000-4000-8000-000000000000', 'confidence_gate', 'Tag manager — score & decide', '{"weights": {"retrieval": 0.55, "draft": 0.1, "groundedness": 0.35}, "default_threshold": 0.5, "tier_overrides": {"basic": 0.5, "premium": 0.6, "enterprise": 0.75}, "escalate_topics": ["billing", "refund", "pricing", "legal", "compliance", "account-access", "data-export", "cancellation"], "escalate_modules": ["Billing & Plans"]}'::jsonb),
  ('f0000000-0000-4000-8000-000000000009', 'f0f0f0f0-0000-4000-8000-000000000000', 'auto_reply', 'Auto-reply to the customer', '{"channel": "email"}'::jsonb),
  ('f0000000-0000-4000-8000-000000000010', 'f0f0f0f0-0000-4000-8000-000000000000', 'ask_human', 'Escalate — ask a human on the Case', '{"channel": "salesforce_chatter", "queue_by_team": {"support": "Support_Tier2", "csm": "Team_CSM", "sales": "Team_Sales", "offboarding": "Team_Offboarding"}, "escalate_queue": "Billing_Escalations"}'::jsonb),
  ('f0000000-0000-4000-8000-000000000011', 'f0f0f0f0-0000-4000-8000-000000000000', 'handover', 'Full handover to the team', '{"reason": "enterprise_or_offboarding", "queue_by_team": {"support": "Support_Tier2", "csm": "Team_CSM", "sales": "Team_Sales", "offboarding": "Team_Offboarding"}, "enterprise_queue": "Enterprise_Support"}'::jsonb)
on conflict (node_id) do nothing;

insert into flow_edges (edge_id, flow_id, source_node_id, target_node_id, condition) values
  ('f0000000-0000-4000-8000-000000000100', 'f0f0f0f0-0000-4000-8000-000000000000', 'f0000000-0000-4000-8000-000000000001', 'f0000000-0000-4000-8000-000000000002', '{}'::jsonb),
  ('f0000000-0000-4000-8000-000000000101', 'f0f0f0f0-0000-4000-8000-000000000000', 'f0000000-0000-4000-8000-000000000002', 'f0000000-0000-4000-8000-000000000003', '{}'::jsonb),
  ('f0000000-0000-4000-8000-000000000102', 'f0f0f0f0-0000-4000-8000-000000000000', 'f0000000-0000-4000-8000-000000000003', 'f0000000-0000-4000-8000-000000000004', '{}'::jsonb),
  ('f0000000-0000-4000-8000-000000000103', 'f0f0f0f0-0000-4000-8000-000000000000', 'f0000000-0000-4000-8000-000000000004', 'f0000000-0000-4000-8000-000000000005', '{}'::jsonb),
  ('f0000000-0000-4000-8000-000000000104', 'f0f0f0f0-0000-4000-8000-000000000000', 'f0000000-0000-4000-8000-000000000005', 'f0000000-0000-4000-8000-000000000006', '{}'::jsonb),
  ('f0000000-0000-4000-8000-000000000105', 'f0f0f0f0-0000-4000-8000-000000000000', 'f0000000-0000-4000-8000-000000000006', 'f0000000-0000-4000-8000-000000000007', '{}'::jsonb),
  ('f0000000-0000-4000-8000-000000000106', 'f0f0f0f0-0000-4000-8000-000000000000', 'f0000000-0000-4000-8000-000000000007', 'f0000000-0000-4000-8000-000000000008', '{}'::jsonb),
  ('f0000000-0000-4000-8000-000000000107', 'f0f0f0f0-0000-4000-8000-000000000000', 'f0000000-0000-4000-8000-000000000008', 'f0000000-0000-4000-8000-000000000009', '{"if": "confidence_gate.pass and tier != ''enterprise'' and routed_team != ''offboarding''"}'::jsonb),
  ('f0000000-0000-4000-8000-000000000108', 'f0f0f0f0-0000-4000-8000-000000000000', 'f0000000-0000-4000-8000-000000000008', 'f0000000-0000-4000-8000-000000000010', '{"if": "not confidence_gate.pass and tier != ''enterprise'' and routed_team != ''offboarding''"}'::jsonb),
  ('f0000000-0000-4000-8000-000000000109', 'f0f0f0f0-0000-4000-8000-000000000000', 'f0000000-0000-4000-8000-000000000008', 'f0000000-0000-4000-8000-000000000011', '{"if": "tier == ''enterprise'' or routed_team == ''offboarding''"}'::jsonb)
on conflict (edge_id) do nothing;

insert into flow_versions (flow_id, version, name, nodes, edges, definition_hash)
select 'f0f0f0f0-0000-4000-8000-000000000000', 1, f.name,
       (select jsonb_agg(jsonb_build_object('node_id',n.node_id,'type',n.type,'label',n.label,'position_x',n.position_x,'position_y',n.position_y,'config',n.config))
        from flow_nodes n where n.flow_id = f.flow_id),
       (select jsonb_agg(jsonb_build_object('edge_id',e.edge_id,'source_node_id',e.source_node_id,'target_node_id',e.target_node_id,'condition',e.condition))
        from flow_edges e where e.flow_id = f.flow_id),
       md5('f0f0f0f0-0000-4000-8000-000000000000-040')
from flows f where f.flow_id = 'f0f0f0f0-0000-4000-8000-000000000000'
on conflict (flow_id, version) do nothing;
