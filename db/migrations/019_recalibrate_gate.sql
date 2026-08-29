-- Phase 7 recalibration: the 011 gate was tuned against the deterministic
-- LLM stub. With a real Groq draft the model's self-graded `draft_confidence`
-- sits ~0.93-0.99 on everything, so at the old 0.5/0.5 retrieval/draft blend
-- it swamped `retrieval_score` and the gate under-escalated (e2e acc 0.909
-- stub -> 0.636 real; all misses premium `ask_human` cases auto-answered).
--
-- Two changes to the Acme "Support L0/L1" gate:
--   1. `weights` — explicit 3-way blend, draft self-confidence weighted low.
--   2. `escalate_topics` — intents that are never a docs answer (billing,
--      refunds, pricing, legal/compliance, account access, data-export
--      requests) force a human regardless of score. Matched on shared slug
--      tokens (see registry._slug_tokens). This is the static down payment
--      on Phase 16's structured, UI-editable policy rules.
--
-- Globex's gate is left alone (it's already "human-review-first", threshold
-- 0.9+, and isn't in the e2e set).

update flow_nodes
set config = jsonb_build_object(
  'default_threshold', 0.5,
  'tier_overrides', jsonb_build_object('basic', 0.5, 'premium', 0.55, 'enterprise', 0.6),
  'weights', jsonb_build_object('retrieval', 0.55, 'draft', 0.1, 'groundedness', 0.35),
  'escalate_topics', jsonb_build_array(
    'billing', 'invoice', 'charge', 'overcharge', 'refund', 'chargeback', 'payment',
    'proration', 'pricing', 'discount', 'quote', 'contract', 'renewal', 'commercial',
    'compliance', 'soc2', 'gdpr', 'legal', 'dpa', 'security-questionnaire',
    'account-access', 'locked-out', 'lockout', '2fa', 'mfa',
    'partner-api', 'data-export', 'account-deletion', 'cancellation', 'offboarding'
  )
)
where flow_id = '11111111-1111-1111-1111-111111111111'
  and type = 'confidence_gate';

-- re-snapshot every published flow so runs pick up the new gate config
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
       md5(f.flow_id::text || '-019')
from flows f
where f.status = 'published';

update flows f
set published_version = (select max(version) from flow_versions v where v.flow_id = f.flow_id),
    version = version + 1
where f.status = 'published';
