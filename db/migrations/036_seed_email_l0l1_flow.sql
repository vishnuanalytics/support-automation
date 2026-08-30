-- Phase 20e: the inbound-email L0/L1 flow — "Email L0/L1 — inbound to Salesforce".
--
-- team `email`, tenant Acme (00000000…). This is the flow the email channel
-- poller (ingestion/email_watch.py) runs against: it turns an inbound message
-- into a real Salesforce Case, triages it, and either auto-replies or raises a
-- human request *on that Case*.
--
--   identify -> sf_case -> retrieve -> classify -> sf_writeback -> draft
--     -> confidence_gate
--          --tier == 'enterprise'-------------------------> handover
--          --pass  & tier != 'enterprise'-----------------> auto_reply
--          --!pass & tier != 'enterprise'-----------------> ask_human  (SF Chatter on the Case)
--
-- `identify` (Phase 17b)  — sender email -> Contact / domain -> Account / unknown.
-- `sf_case`  (Phase 20e)  — create-or-reuse the Salesforce Case; writes `case.sf_id`
--                           and the Account tier/region back into state so
--                           `classify` gates on the real customer tier.
-- `classify` here carries `default_tier: "basic"` — a brand-new sender whose
-- Account we just created has no `Tier__c`, and the global fallback for an
-- unknown tier is `enterprise` (always-handover); `basic` lets L0/L1 mail
-- actually get an auto-reply. An existing Account's real tier still wins.
-- The three confidence_gate conditions are mutually exclusive + exhaustive
-- (tier ∈ {basic,premium,enterprise}; gate.pass ∈ {true,false}), so routing
-- does not depend on edge order.
--
-- No code change for the node types (`type` is a free string; handlers are
-- interpreter/registry.h_identify / h_sf_case). Portable copy:
-- interpreter/flows/flow_email_l0l1.json.

insert into flows (flow_id, tenant_id, team, name, status, version, published_version)
values ('e5e5e5e5-5555-4555-8555-555555555555',
        '00000000-0000-0000-0000-000000000000',
        'email', 'Email L0/L1 — inbound to Salesforce', 'published', 1, 1)
on conflict (flow_id) do nothing;

insert into flow_nodes (node_id, flow_id, type, label, position_x, position_y, config) values
 ('e5000001-5555-4555-8555-555555555555', 'e5e5e5e5-5555-4555-8555-555555555555',
  'identify', 'Resolve the sender', 0, 100,
  '{"email_field": "from", "domain_match": true, "create_lead_if_missing": false}'::jsonb),
 ('e5000002-5555-4555-8555-555555555555', 'e5e5e5e5-5555-4555-8555-555555555555',
  'sf_case', 'Create / reuse the Salesforce Case', 200, 100,
  '{"origin": "Email", "status": "New", "create_contact": true, "create_account": true, "reuse_open_days": 14}'::jsonb),
 ('e5000003-5555-4555-8555-555555555555', 'e5e5e5e5-5555-4555-8555-555555555555',
  'retrieve', 'Retrieve KB context', 400, 100,
  '{"source": ["supabase"], "top_k": 5, "use_rerank": true}'::jsonb),
 ('e5000004-5555-4555-8555-555555555555', 'e5e5e5e5-5555-4555-8555-555555555555',
  'classify', 'Classify tier / topic / urgency', 600, 100,
  '{"tier_field": "account.customer_type", "region_field": "account.region", "default_tier": "basic"}'::jsonb),
 ('e5000005-5555-4555-8555-555555555555', 'e5e5e5e5-5555-4555-8555-555555555555',
  'sf_writeback', 'Write triage fields to the Case', 800, 100, '{}'::jsonb),
 ('e5000006-5555-4555-8555-555555555555', 'e5e5e5e5-5555-4555-8555-555555555555',
  'draft', 'Draft the reply from context', 1000, 40,
  '{"model": "openai/gpt-oss-120b", "max_tokens": 700}'::jsonb),
 ('e5000007-5555-4555-8555-555555555555', 'e5e5e5e5-5555-4555-8555-555555555555',
  'confidence_gate', 'Gate on answer quality', 1200, 40,
  '{"weights": {"retrieval": 0.55, "draft": 0.1, "groundedness": 0.35},
    "default_threshold": 0.5,
    "tier_overrides": {"basic": 0.5, "premium": 0.6, "enterprise": 0.75},
    "escalate_topics": ["billing", "refund", "pricing", "legal", "compliance",
                        "account-access", "data-export", "partner-api", "cancellation"]}'::jsonb),
 ('e5000008-5555-4555-8555-555555555555', 'e5e5e5e5-5555-4555-8555-555555555555',
  'auto_reply', 'Send the drafted reply', 1400, 0,
  '{"channel": "email"}'::jsonb),
 ('e5000009-5555-4555-8555-555555555555', 'e5e5e5e5-5555-4555-8555-555555555555',
  'ask_human', 'Raise a human request on the Case', 1400, 140,
  '{"channel": "salesforce_chatter", "reason": "below_confidence_or_forced_escalation"}'::jsonb),
 ('e500000a-5555-4555-8555-555555555555', 'e5e5e5e5-5555-4555-8555-555555555555',
  'handover', 'Full handover (enterprise)', 1200, 220,
  '{"reason": "enterprise_tier"}'::jsonb)
on conflict (node_id) do nothing;

insert into flow_edges (edge_id, flow_id, source_node_id, target_node_id, condition) values
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000001-5555-4555-8555-555555555555', 'e5000002-5555-4555-8555-555555555555', '{}'::jsonb),
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000002-5555-4555-8555-555555555555', 'e5000003-5555-4555-8555-555555555555', '{}'::jsonb),
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000003-5555-4555-8555-555555555555', 'e5000004-5555-4555-8555-555555555555', '{}'::jsonb),
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000004-5555-4555-8555-555555555555', 'e5000005-5555-4555-8555-555555555555', '{}'::jsonb),
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000005-5555-4555-8555-555555555555', 'e5000006-5555-4555-8555-555555555555', '{}'::jsonb),
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000006-5555-4555-8555-555555555555', 'e5000007-5555-4555-8555-555555555555', '{}'::jsonb),
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000007-5555-4555-8555-555555555555', 'e500000a-5555-4555-8555-555555555555',
  '{"if": "tier == ''enterprise''"}'::jsonb),
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000007-5555-4555-8555-555555555555', 'e5000008-5555-4555-8555-555555555555',
  '{"if": "confidence_gate.pass and tier != ''enterprise''"}'::jsonb),
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000007-5555-4555-8555-555555555555', 'e5000009-5555-4555-8555-555555555555',
  '{"if": "not confidence_gate.pass and tier != ''enterprise''"}'::jsonb)
on conflict (edge_id) do nothing;

-- v1 snapshot so `status='published'` runs load from flow_versions
insert into flow_versions (flow_id, version, name, nodes, edges, definition_hash)
select 'e5e5e5e5-5555-4555-8555-555555555555', 1, f.name,
       (select jsonb_agg(jsonb_build_object('node_id', n.node_id, 'type', n.type, 'label', n.label,
               'position_x', n.position_x, 'position_y', n.position_y, 'config', n.config))
        from flow_nodes n where n.flow_id = f.flow_id),
       (select jsonb_agg(jsonb_build_object('edge_id', e.edge_id, 'source_node_id', e.source_node_id,
               'target_node_id', e.target_node_id, 'condition', e.condition))
        from flow_edges e where e.flow_id = f.flow_id),
       md5('e5e5e5e5-5555-4555-8555-555555555555-036')
from flows f where f.flow_id = 'e5e5e5e5-5555-4555-8555-555555555555'
on conflict (flow_id, version) do nothing;
