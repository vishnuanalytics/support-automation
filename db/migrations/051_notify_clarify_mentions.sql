-- Phase 23c: make `clarify` / `notify` @mention a real person on the Case.
--
-- Chatter can't @mention a Queue group, so:
--   clarify.mention_team = true  -> mention a member of Team_<routed_team>
--   *.mention_id                 -> the fallback user when no queue member
--                                  resolves (here: Gundam Vishnu)
--
-- Email flow (e5e5e5e5…). Config-only; re-snapshots the published version.

update flow_nodes
   set config = config || '{"mention_team": true, "mention_id": "005jV000000fm5WQAQ"}'::jsonb
 where node_id = 'e500000c-5555-4555-8555-555555555555';   -- clarify

update flow_nodes
   set config = config || '{"mention_id": "005jV000000fm5WQAQ"}'::jsonb
 where node_id = 'e500000b-5555-4555-8555-555555555555';   -- notify

update flow_versions v
   set nodes = (select jsonb_agg(jsonb_build_object(
                  'node_id', n.node_id, 'type', n.type, 'label', n.label,
                  'position_x', n.position_x, 'position_y', n.position_y, 'config', n.config))
                from flow_nodes n where n.flow_id = v.flow_id)
 where v.flow_id = 'e5e5e5e5-5555-4555-8555-555555555555'
   and v.version = (select published_version from flows
                    where flow_id = 'e5e5e5e5-5555-4555-8555-555555555555');
