-- Phase 20g: wire the flows to the Salesforce routing queues + Case
-- picklists created by scripts/sf_support_setup.py.
--
--  * ask_human / handover nodes get a `queue` (and ask_human an
--    `escalate_queue`, used when the gate forced the escalation on topic).
--  * the "Support L0/L1" flow's sf_writeback field_map is repointed from the
--    old free-text Module__c/Region__c to the new picklists +
--    Module__c / SubModule__c / Region__c / Topic__c (the email flow's
--    sf_writeback is `{}` and picks up the new picklist-aware code default).
--
-- Queues: Support_L0L1, Billing_Escalations, Enterprise_Support, Support_Tier2
-- (+ per-team queues). Config-only; re-snapshots each published version.

-- ── email L0/L1 (e5e5e5e5…) ────────────────────────────────────────────
update flow_nodes set config = config
    || '{"queue": "Support_L0L1", "escalate_queue": "Billing_Escalations"}'::jsonb
 where node_id = 'e5000009-5555-4555-8555-555555555555';           -- ask_human
update flow_nodes set config = config || '{"queue": "Enterprise_Support"}'::jsonb
 where node_id = 'e500000a-5555-4555-8555-555555555555';           -- handover

-- ── Support L0/L1 tier-gated (11111111…) ──────────────────────────────
update flow_nodes set config = config
    || '{"queue": "Support_Tier2", "escalate_queue": "Billing_Escalations"}'::jsonb
 where node_id = '7c18d8da-3147-52df-9c74-d8a321644e7c';           -- ask_human
update flow_nodes set config = config || '{"queue": "Enterprise_Support"}'::jsonb
 where node_id = '1ffdcc48-0b89-5483-92b1-6ddea60121b0';           -- handover
update flow_nodes set config = jsonb_set(config, '{field_map}',
    '{"urgency": "Priority", "case_topic": "Topic__c", "case_module": "Module__c",
      "case_submodule": "SubModule__c", "case_region": "Region__c"}'::jsonb)
 where node_id = '3b9a1f2c-5d6e-4f70-8a1b-000000000008';           -- sf_writeback

-- ── retrieval-gated triage (d4d4d4d4…) ───────────────────────────────
update flow_nodes set config = config
    || '{"queue": "Support_Tier2", "escalate_queue": "Billing_Escalations"}'::jsonb
 where node_id = 'd4000007-4444-4444-8444-444444444444';           -- ask_human
update flow_nodes set config = config || '{"queue": "Enterprise_Support"}'::jsonb
 where node_id = 'd4000008-4444-4444-8444-444444444444';           -- handover

-- ── re-snapshot each flow's published version from the updated draft ──
update flow_versions v
   set nodes = (select jsonb_agg(jsonb_build_object(
                  'node_id', n.node_id, 'type', n.type, 'label', n.label,
                  'position_x', n.position_x, 'position_y', n.position_y, 'config', n.config))
                from flow_nodes n where n.flow_id = v.flow_id)
 from flows f
where f.flow_id = v.flow_id
  and v.version = f.published_version
  and f.flow_id in ('e5e5e5e5-5555-4555-8555-555555555555',
                    '11111111-1111-1111-1111-111111111111',
                    'd4d4d4d4-4444-4444-8444-444444444444');
