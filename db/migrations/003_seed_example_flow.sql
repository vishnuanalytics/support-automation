-- Phase 0 seed: your tenant_members row + the example Support flow

insert into tenant_members (user_id, tenant_id, role) values
  ('4ddf2413-6ccc-4c5e-9da6-b5c3c0391941', '00000000-0000-0000-0000-000000000000', 'owner')
on conflict (user_id, tenant_id) do nothing;

insert into flows (flow_id, tenant_id, team, name, version, status) values
  ('11111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-000000000000', 'support', 'Support L0/L1 - tier-gated flow', 1, 'draft')
on conflict (flow_id) do nothing;

insert into flow_nodes (node_id, flow_id, type, label, config) values
  ('61ac57b5-34b3-5097-afd4-e9cdbf00b245', '11111111-1111-1111-1111-111111111111', 'retrieve', 'Retrieve context', '{"source": ["supabase", "neo4j"], "top_k": 5}'::jsonb),
  ('e4f01f03-cbf2-5f1a-8e0f-ad83ac8e11c5', '11111111-1111-1111-1111-111111111111', 'classify', 'Classify customer', '{"tier_field": "account.customer_type", "region_field": "account.region"}'::jsonb),
  ('2e18fa56-a635-530b-a5be-7b91eb6ba683', '11111111-1111-1111-1111-111111111111', 'draft', 'Draft reply', '{"model": "llama-3.3-70b-versatile", "max_tokens": 500}'::jsonb),
  ('7f6c96dc-273f-55cf-9d1f-5519045c839c', '11111111-1111-1111-1111-111111111111', 'confidence_gate', 'Confidence gate', '{"default_threshold": 0.35, "tier_overrides": {"basic": 0.35, "premium": 0.45, "enterprise": 0.6}}'::jsonb),
  ('46ae7c37-521c-5d17-925a-a80e3f7cd88e', '11111111-1111-1111-1111-111111111111', 'auto_reply', 'Auto-reply', '{}'::jsonb),
  ('7c18d8da-3147-52df-9c74-d8a321644e7c', '11111111-1111-1111-1111-111111111111', 'ask_human', 'Ask human', '{"channel": "salesforce_chatter"}'::jsonb),
  ('1ffdcc48-0b89-5483-92b1-6ddea60121b0', '11111111-1111-1111-1111-111111111111', 'handover', 'Full handover', '{"reason": "enterprise_or_low_confidence"}'::jsonb);

insert into flow_edges (source_node_id, target_node_id, flow_id, condition) values
  ('61ac57b5-34b3-5097-afd4-e9cdbf00b245', 'e4f01f03-cbf2-5f1a-8e0f-ad83ac8e11c5', '11111111-1111-1111-1111-111111111111', '{}'::jsonb),
  ('e4f01f03-cbf2-5f1a-8e0f-ad83ac8e11c5', '2e18fa56-a635-530b-a5be-7b91eb6ba683', '11111111-1111-1111-1111-111111111111', '{}'::jsonb),
  ('2e18fa56-a635-530b-a5be-7b91eb6ba683', '7f6c96dc-273f-55cf-9d1f-5519045c839c', '11111111-1111-1111-1111-111111111111', '{}'::jsonb),
  ('7f6c96dc-273f-55cf-9d1f-5519045c839c', '46ae7c37-521c-5d17-925a-a80e3f7cd88e', '11111111-1111-1111-1111-111111111111', '{"if": "confidence_gate.pass and tier != ''enterprise''"}'::jsonb),
  ('7f6c96dc-273f-55cf-9d1f-5519045c839c', '7c18d8da-3147-52df-9c74-d8a321644e7c', '11111111-1111-1111-1111-111111111111', '{"if": "not confidence_gate.pass and tier != ''enterprise''"}'::jsonb),
  ('7f6c96dc-273f-55cf-9d1f-5519045c839c', '1ffdcc48-0b89-5483-92b1-6ddea60121b0', '11111111-1111-1111-1111-111111111111', '{"if": "tier == ''enterprise''"}'::jsonb);
