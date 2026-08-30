-- Phase 20h: wire the queues into the flows migration 038 missed
-- (Globex support, catalog/csm, offboarding, Slack-approval), and give the
-- Globex sf_writeback the same Case-picklist field_map the other flows use.
--
-- Queues (scripts/sf_support_setup.py): Team_CSM, Team_Offboarding,
-- Support_Tier2, Billing_Escalations, Enterprise_Support.
-- Config-only; re-snapshots each flow's published version.

-- ── Globex support (a2a2a2a2…, tenant Globex) ─────────────────────────
update flow_nodes set config = config
    || '{"queue": "Support_Tier2", "escalate_queue": "Billing_Escalations"}'::jsonb
 where node_id = 'a2000007-2222-4222-8222-222222222222';           -- ask_human
update flow_nodes set config = config || '{"queue": "Enterprise_Support"}'::jsonb
 where node_id = 'a2000008-2222-4222-8222-222222222222';           -- handover
update flow_nodes set config = jsonb_set(config, '{field_map}',
    '{"urgency": "Priority", "case_topic": "Topic__c", "case_module": "Module__c",
      "case_submodule": "SubModule__c", "case_region": "Region__c"}'::jsonb)
 where node_id = 'a2000003-2222-4222-8222-222222222222';           -- sf_writeback

-- ── catalog / csm (a4f1e382…) ────────────────────────────────────────
update flow_nodes set config = config || '{"queue": "Team_CSM"}'::jsonb
 where node_id = '2b308232-bfcf-4df2-8383-175049782075';           -- ask_human

-- ── offboarding (c3c3c3c3…) ──────────────────────────────────────────
update flow_nodes set config = config || '{"queue": "Team_Offboarding"}'::jsonb
 where node_id = 'c3000004-3333-4333-8333-333333333333';           -- handover

-- ── Slack-approval (781cf1cc…) ───────────────────────────────────────
update flow_nodes set config = config
    || '{"queue": "Support_Tier2", "escalate_queue": "Billing_Escalations"}'::jsonb
 where node_id = '4cb80a7b-996c-40d1-9bed-b9678f8ac134';           -- ask_human

-- ── re-snapshot each affected flow's published version ───────────────
update flow_versions v
   set nodes = (select jsonb_agg(jsonb_build_object(
                  'node_id', n.node_id, 'type', n.type, 'label', n.label,
                  'position_x', n.position_x, 'position_y', n.position_y, 'config', n.config))
                from flow_nodes n where n.flow_id = v.flow_id)
 from flows f
where f.flow_id = v.flow_id
  and v.version = f.published_version
  and f.flow_id in ('a2a2a2a2-2222-4222-8222-222222222222',
                    'a4f1e382-403c-452e-9a3f-2f4ac4442bd6',
                    'c3c3c3c3-3333-4333-8333-333333333333',
                    '781cf1cc-2750-4d4d-8e0b-3a7d9ca7117c');
