-- Phase 4: multi-tenant / multi-flow seed.
--
-- Proves the interpreter runs *different agents from different data* through
-- one build_graph. After this migration there are three published flows:
--
--   A) tenant 0000…  team 'support'      flow 1111…  (Phase 0/2/3 flow, now published)
--        retrieve -> classify -> sf_writeback -> draft -> gate -> {auto_reply|ask_human|handover}
--        per-tier gate {basic .35, premium .45, enterprise .6}; full SF field map
--
--   B) tenant 2222…  team 'support'      flow a2a2…  (NEW)
--        same topology, but a *strict* gate {basic .9, premium .95, enterprise .99}
--        so nothing auto-sends, a minimal SF map (Priority + Description only),
--        no Neo4j expansion, the small 8B model. Same team name as A, different
--        tenant -> allowed by uq_one_published_flow_per_team (it's per tenant+team).
--
--   C) tenant 0000…  team 'offboarding'  flow c3c3…  (NEW)
--        different *topology*: retrieve -> classify -> draft -> handover.
--        No gate, no sf_writeback, no auto-reply -- every case goes to a human.
--
-- Same input case through A/B/C yields auto_reply / ask_human / handover
-- respectively -- three behaviours, zero code differences.
--
-- Auth users are created out of band (like 4ddf2413 was, via signup): the
-- synthetic tenant-B owner b2b20000-0000-4000-8000-000000000002 was inserted
-- into auth.users via the SQL editor. This migration only seeds tenant_members
-- + flows, matching 003's pattern.

-- ── A) publish the existing tenant-A support flow ────────────────────────
update flows set status = 'published', updated_at = now()
where flow_id = '11111111-1111-1111-1111-111111111111';

-- ── tenant B membership ────────────────────────────────────────────────
insert into tenant_members (user_id, tenant_id, role) values
  ('b2b20000-0000-4000-8000-000000000002', '22222222-2222-2222-2222-222222222222', 'owner')
on conflict (user_id, tenant_id) do nothing;

-- ── B) tenant-B support flow: human-review-first ───────────────────────
insert into flows (flow_id, tenant_id, team, name, version, status) values
  ('a2a2a2a2-2222-4222-8222-222222222222', '22222222-2222-2222-2222-222222222222',
   'support', 'Globex Support — human-review-first', 1, 'published')
on conflict (flow_id) do nothing;

insert into flow_nodes (node_id, flow_id, type, label, config) values
  ('a2000001-2222-4222-8222-222222222222', 'a2a2a2a2-2222-4222-8222-222222222222',
   'retrieve', 'Retrieve context',
   '{"source": ["supabase"], "top_k": 3, "use_graph": false}'::jsonb),
  ('a2000002-2222-4222-8222-222222222222', 'a2a2a2a2-2222-4222-8222-222222222222',
   'classify', 'Classify customer',
   '{"tier_field": "account.customer_type", "region_field": "account.region", "model": "llama-3.1-8b-instant"}'::jsonb),
  ('a2000003-2222-4222-8222-222222222222', 'a2a2a2a2-2222-4222-8222-222222222222',
   'sf_writeback', 'Write triage to Salesforce',
   '{"object": "Case", "field_map": {"urgency": "Priority"}, "value_maps": {"Priority": {"critical": "High", "high": "High", "normal": "Medium", "low": "Low"}}, "append": {"Description": "summary"}}'::jsonb),
  ('a2000004-2222-4222-8222-222222222222', 'a2a2a2a2-2222-4222-8222-222222222222',
   'draft', 'Draft reply',
   '{"model": "llama-3.1-8b-instant", "max_tokens": 400}'::jsonb),
  ('a2000005-2222-4222-8222-222222222222', 'a2a2a2a2-2222-4222-8222-222222222222',
   'confidence_gate', 'Confidence gate (strict)',
   '{"default_threshold": 0.9, "tier_overrides": {"basic": 0.9, "premium": 0.95, "enterprise": 0.99}, "retrieval_weight": 0.5}'::jsonb),
  ('a2000006-2222-4222-8222-222222222222', 'a2a2a2a2-2222-4222-8222-222222222222',
   'auto_reply', 'Auto-reply', '{}'::jsonb),
  ('a2000007-2222-4222-8222-222222222222', 'a2a2a2a2-2222-4222-8222-222222222222',
   'ask_human', 'Ask human', '{"channel": "salesforce_chatter"}'::jsonb),
  ('a2000008-2222-4222-8222-222222222222', 'a2a2a2a2-2222-4222-8222-222222222222',
   'handover', 'Full handover', '{"reason": "enterprise_or_low_confidence"}'::jsonb)
on conflict (node_id) do nothing;

insert into flow_edges (source_node_id, target_node_id, flow_id, condition) values
  ('a2000001-2222-4222-8222-222222222222', 'a2000002-2222-4222-8222-222222222222', 'a2a2a2a2-2222-4222-8222-222222222222', '{}'::jsonb),
  ('a2000002-2222-4222-8222-222222222222', 'a2000003-2222-4222-8222-222222222222', 'a2a2a2a2-2222-4222-8222-222222222222', '{}'::jsonb),
  ('a2000003-2222-4222-8222-222222222222', 'a2000004-2222-4222-8222-222222222222', 'a2a2a2a2-2222-4222-8222-222222222222', '{}'::jsonb),
  ('a2000004-2222-4222-8222-222222222222', 'a2000005-2222-4222-8222-222222222222', 'a2a2a2a2-2222-4222-8222-222222222222', '{}'::jsonb),
  ('a2000005-2222-4222-8222-222222222222', 'a2000006-2222-4222-8222-222222222222', 'a2a2a2a2-2222-4222-8222-222222222222', '{"if": "confidence_gate.pass and tier != ''enterprise''"}'::jsonb),
  ('a2000005-2222-4222-8222-222222222222', 'a2000007-2222-4222-8222-222222222222', 'a2a2a2a2-2222-4222-8222-222222222222', '{"if": "not confidence_gate.pass and tier != ''enterprise''"}'::jsonb),
  ('a2000005-2222-4222-8222-222222222222', 'a2000008-2222-4222-8222-222222222222', 'a2a2a2a2-2222-4222-8222-222222222222', '{"if": "tier == ''enterprise''"}'::jsonb)
on conflict do nothing;

-- ── C) tenant-A offboarding flow: always handover ─────────────────────
insert into flows (flow_id, tenant_id, team, name, version, status) values
  ('c3c3c3c3-3333-4333-8333-333333333333', '00000000-0000-0000-0000-000000000000',
   'offboarding', 'Acme Offboarding — always handover', 1, 'published')
on conflict (flow_id) do nothing;

insert into flow_nodes (node_id, flow_id, type, label, config) values
  ('c3000001-3333-4333-8333-333333333333', 'c3c3c3c3-3333-4333-8333-333333333333',
   'retrieve', 'Retrieve context', '{"source": ["supabase"], "top_k": 4}'::jsonb),
  ('c3000002-3333-4333-8333-333333333333', 'c3c3c3c3-3333-4333-8333-333333333333',
   'classify', 'Classify customer',
   '{"tier_field": "account.customer_type", "region_field": "account.region"}'::jsonb),
  ('c3000003-3333-4333-8333-333333333333', 'c3c3c3c3-3333-4333-8333-333333333333',
   'draft', 'Draft context note', '{"model": "llama-3.1-8b-instant", "max_tokens": 300}'::jsonb),
  ('c3000004-3333-4333-8333-333333333333', 'c3c3c3c3-3333-4333-8333-333333333333',
   'handover', 'Hand to account owner', '{"reason": "offboarding_requires_account_owner"}'::jsonb)
on conflict (node_id) do nothing;

insert into flow_edges (source_node_id, target_node_id, flow_id, condition) values
  ('c3000001-3333-4333-8333-333333333333', 'c3000002-3333-4333-8333-333333333333', 'c3c3c3c3-3333-4333-8333-333333333333', '{}'::jsonb),
  ('c3000002-3333-4333-8333-333333333333', 'c3000003-3333-4333-8333-333333333333', 'c3c3c3c3-3333-4333-8333-333333333333', '{}'::jsonb),
  ('c3000003-3333-4333-8333-333333333333', 'c3000004-3333-4333-8333-333333333333', 'c3c3c3c3-3333-4333-8333-333333333333', '{}'::jsonb)
on conflict do nothing;
