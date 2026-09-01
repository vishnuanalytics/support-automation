-- Phase 23e: stop the double Chatter tag on an escalated Case.
--
-- Before: `ask_human` posted its own @mention Chatter note + a private draft
-- CaseComment, AND the downstream `notify_human` posted a second @mention note
-- → the rep was tagged twice and the feed showed 3 bot posts.
--
-- After: `ask_human` is routing-only (`post_note: false` — it still reassigns
-- the queue), and `notify_human` owns the single human ping. The reviewable
-- draft is preserved by `draft_comment: true` on `notify_human`, which drops it
-- as one private CaseComment.
--
-- Email flow (e5e5e5e5…). Publishes the next version.

update flow_nodes
   set config = config || '{"post_note": false}'::jsonb
 where node_id = 'e5000011-5555-4555-8555-555555555555';

update flow_nodes
   set config = config || '{"draft_comment": true}'::jsonb
 where node_id = 'e5000013-5555-4555-8555-555555555555';

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
    md5(fid::text || '-053')
  from flows f where f.flow_id = fid
  on conflict (flow_id, version) do nothing;
  update flows set version = nextv, published_version = nextv where flow_id = fid;
end $$;
