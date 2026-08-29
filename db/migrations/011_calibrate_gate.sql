-- Phase 7: calibrate the Acme support flow's confidence gate from the
-- end-to-end eval (eval/e2e/run_e2e.py, 22 cases).
--
-- Before: default_threshold 0.35, per-tier {basic .35, premium .45,
--   enterprise .6}, retrieval_weight .5.  -> action acc 0.864,
--   auto-send precision 0.769 (3/13 unsafe auto-sends), escalation 1.00.
--
-- The threshold sweep showed auto-send precision rising 0.71 -> 0.83 as the
-- bar moves 0.35 -> 0.55, coverage 0.64 -> 0.55. Add a small
-- groundedness_weight so a relevant-but-thin draft is penalised (fixes the
-- data-export case). The two remaining unsafe auto-sends (SOC2 / Partner
-- API) are commercial/legal *intents* — a threshold can't catch them; that
-- needs an intent -> ask_human edge (follow-up, not this migration).

update flow_nodes
set config = jsonb_build_object(
  'default_threshold',   0.5,
  'tier_overrides',      jsonb_build_object('basic', 0.5, 'premium', 0.55, 'enterprise', 0.6),
  'retrieval_weight',    0.5,
  'groundedness_weight', 0.2
)
where flow_id = '11111111-1111-1111-1111-111111111111'
  and type = 'confidence_gate';
