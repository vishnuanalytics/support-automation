-- Fix a pre-existing bug in 008_seed_sf_writeback_node.sql's seed config,
-- found while restoring this project's missing Acme seed flows (003/008/
-- 009/019 had never been applied here -- see PROJECT_SCOPE.md's Known
-- issues for the restoration note).
--
-- 008's sf_writeback node config mapped the classifier's RAW `topic` slug
-- straight onto the restricted `Module__c` picklist (`field_map:
-- {"topic": "Module__c", ...}`). `Module__c` only accepts a fixed set of
-- values (scripts/sf_support_setup.py), so an unmapped topic like
-- "webhook-testing" gets rejected by Salesforce
-- (INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST). The node's own DEFAULT
-- field_map (interpreter/registry.py::h_sf_writeback, used when a flow
-- doesn't override it -- e.g. Globex's flow) already does this safely via
-- `case_module`/`case_region`, the salesforce.map_case_fields()-derived
-- picklist-safe values. 008 predates that derivation and was never updated
-- to use it.
--
-- Not editing 008 in place (CLAUDE.md: migrations are sequential,
-- single-concern, never amended) -- this corrects the same node going
-- forward on any environment (fresh or already-seeded) via ON CONFLICT-free
-- idempotent UPDATEs.

update flow_nodes
set config = jsonb_set(
  config,
  '{field_map}',
  '{"urgency": "Priority", "case_module": "Module__c", "case_region": "Region__c"}'::jsonb
)
where node_id = '3b9a1f2c-5d6e-4f70-8a1b-000000000008'
  and flow_id = '11111111-1111-1111-1111-111111111111';

-- re-snapshot + republish so a run actually picks up the fixed config
-- (published flows execute the flow_versions snapshot, not the live draft).
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
       md5(f.flow_id::text || '-079-sf-writeback-fix')
from flows f
where f.flow_id = '11111111-1111-1111-1111-111111111111'
  and not exists (
    select 1 from flow_versions v
    where v.flow_id = f.flow_id and v.definition_hash = md5(f.flow_id::text || '-079-sf-writeback-fix')
  );

update flows
set published_version = (select max(version) from flow_versions where flow_id = '11111111-1111-1111-1111-111111111111'),
    version = version + 1
where flow_id = '11111111-1111-1111-1111-111111111111'
  and status = 'published'
  and published_version < (select max(version) from flow_versions where flow_id = '11111111-1111-1111-1111-111111111111');
