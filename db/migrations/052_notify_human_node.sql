-- Phase 23d: `notify_human` — the workflow (not hard-coded config) tags a
-- person about an escalated Case, on Slack and/or Salesforce Chatter.
--
--   … confidence_gate ─ ask_human  ─┐
--                     └ handover    ┴─→ notify_human   (terminal, pass-through)
--
-- ask_human / handover keep doing the Case routing (reassign queue); the new
-- node does the actual human ping. `channel` = both | slack | salesforce_chatter.
-- Slack uses the tenant bot token + a channel if set, else SLACK_ALERT_WEBHOOK.
-- Chatter @mentions the resolved rep (a member of Team_<routed_team>, else
-- `mention.mention_id`). Email flow (e5e5e5e5…). Publishes the next version.

insert into flow_nodes (node_id, flow_id, type, label, position_x, position_y, config) values
 ('e5000013-5555-4555-8555-555555555555', 'e5e5e5e5-5555-4555-8555-555555555555',
  'notify_human', 'Tag a human (Slack + / or Chatter)', 1650, 300,
  '{"channel": "both",
    "slack_channel": "#support-escalations",
    "slack_channel_by_team": {"csm": "#csm-escalations", "sales": "#sales-escalations",
                              "offboarding": "#retention", "default": "#support-escalations"},
    "mention": {"mention_id": "005jV000000fm5WQAQ"}}'::jsonb)
on conflict (node_id) do nothing;

insert into flow_edges (edge_id, flow_id, source_node_id, target_node_id, condition) values
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e5000011-5555-4555-8555-555555555555', 'e5000013-5555-4555-8555-555555555555', '{}'::jsonb),
 (gen_random_uuid(), 'e5e5e5e5-5555-4555-8555-555555555555',
  'e500000a-5555-4555-8555-555555555555', 'e5000013-5555-4555-8555-555555555555', '{}'::jsonb);

do $$
declare
  fid uuid := 'e5e5e5e5-5555-4555-8555-555555555555';
  nextv int;
begin
  select coalesce(published_version, version, 1) + 1 into nextv from flows where flow_id = fid;
  insert into flow_versions (flow_id, version, name, nodes, edges, definition_hash)
  select fid, nextv, f.name,
    (select jsonb_agg(jsonb_build_object('node_id',n.node_id,'type',n.type,'label',n.label,
            'position_x',n.position_x,'position_y',n.position_y,'config',n.config))
     from flow_nodes n where n.flow_id = fid),
    (select jsonb_agg(jsonb_build_object('edge_id',e.edge_id,'source_node_id',e.source_node_id,
            'target_node_id',e.target_node_id,'condition',e.condition))
     from flow_edges e where e.flow_id = fid),
    md5(fid::text || '-052')
  from flows f where f.flow_id = fid
  on conflict (flow_id, version) do nothing;
  update flows set version = nextv, published_version = nextv where flow_id = fid;
end $$;
