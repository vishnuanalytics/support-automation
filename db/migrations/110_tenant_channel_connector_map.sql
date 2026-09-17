-- Multi-provider connectors: a per-tenant CHANNEL -> connector map, so one
-- flow definition can route different case-touching nodes to different
-- connectors automatically, based on which channel a case actually arrived
-- on (case.channel: 'email' / 'hubspot' / 'freshchat' / 'zendesk' / ...) --
-- no flow duplication, no explicit branch nodes needed.
--
-- Supersedes an earlier same-day stopgap (a `team="hubspot"` convention
-- read by `hubspot.resolve_entry_flow`, and a fully duplicated second flow
-- graph) that turned out not to fit this project's real flow shape: a
-- tenant's case-touching nodes (sf_case/sf_writeback/identify/clarify/
-- notify/ask_human/handover/notify_human) are scattered from the very
-- start of a flow to its very end, not clustered at one branch point --
-- duplicating the graph to fork on channel would have meant re-duplicating
-- almost the entire flow. This is the simpler, more correct fix: resolve
-- the connector per case-touching node, per call, from the case's own
-- channel -- zero duplication, zero explicit graph branching.
--
-- `connectors.resolve_case_connector()`'s precedence becomes: an explicit
-- per-node `config.connector` override (unchanged, highest priority) >
-- this map, keyed by `case.channel`, when the calling node's flow state has
-- one > the tenant's existing `case_connector` default (unchanged) >
-- 'salesforce' (unchanged). A tenant with an empty map (the default)
-- behaves byte-for-byte as before this migration.
--
-- jsonb, default '{}' -- same "data, not an enum" philosophy as
-- `case_connector` (084) and `flow_nodes.type`: no CHECK constraint on the
-- channel names or connector slugs, since both sets grow as connectors and
-- channels are added.

alter table tenants
  add column if not exists channel_connector_map jsonb not null default '{}'::jsonb;

comment on column tenants.channel_connector_map is
  'Maps a case''s channel (case.channel: email/hubspot/freshchat/zendesk/...) '
  'to the connector slug (interpreter/connectors.py) case-touching node '
  'handlers should invoke for that channel, when the calling node has no '
  'per-node `connector` override. Empty map (default) means every channel '
  'falls through to `case_connector`. No CHECK constraint on purpose -- '
  'channels and connectors are both data.';
