-- Phase 20f: the email L0/L1 flow (e5e5e5e5…) — switch `sf_case` from
-- time-window Case reuse to **thread-based** reuse (FR-6).
--
-- Before: `reuse_open_days: 14` — any open Case for the sender within 14
-- days was reused, so unrelated follow-ups piled onto one Case.
-- After:  `reuse: "thread"` — a new email attaches to an open Case only
-- when its In-Reply-To / References match an EmailMessage already on that
-- Case (`salesforce.find_case_by_thread`). A genuinely new subject → a new
-- Case.
--
-- No node/edge topology change; config-only. Re-snapshots published v1.
-- Portable copy: interpreter/flows/flow_email_l0l1.json.

update flow_nodes
   set config = jsonb_build_object(
         'origin', 'Email', 'status', 'New',
         'create_contact', true, 'create_account', true,
         'reuse', 'thread')
 where node_id = 'e5000002-5555-4555-8555-555555555555';

-- re-snapshot published v1 from the updated draft graph
update flow_versions v
   set nodes = (select jsonb_agg(jsonb_build_object(
                  'node_id', n.node_id, 'type', n.type, 'label', n.label,
                  'position_x', n.position_x, 'position_y', n.position_y, 'config', n.config))
                from flow_nodes n where n.flow_id = v.flow_id)
 where v.flow_id = 'e5e5e5e5-5555-4555-8555-555555555555' and v.version = 1;
