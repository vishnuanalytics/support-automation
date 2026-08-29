-- Phase 3: add the `sf_writeback` node to the Phase 0 seed Support flow.
--
--   classify -> draft   becomes   classify -> sf_writeback -> draft
--
-- The node writes triage output (urgency/topic/region + the case summary)
-- back onto the Salesforce Case. Behaviour is entirely in `config` -- a
-- field map, per-field value maps, and an append map -- so Phase 5's UI can
-- edit it without code. The interpreter's registry maps type
-- 'sf_writeback' -> handler; no schema change, matching the generic-`type`
-- design.
--
-- Custom Case fields the default config references (`Module__c`, `Region__c`):
-- see SALESFORCE_SETUP.md. If they don't exist the write is tolerant --
-- the node drops the unknown field and still writes the rest.

insert into flow_nodes (node_id, flow_id, type, label, config) values
  ('3b9a1f2c-5d6e-4f70-8a1b-000000000008',
   '11111111-1111-1111-1111-111111111111',
   'sf_writeback',
   'Write triage to Salesforce',
   '{
      "object": "Case",
      "field_map": {"urgency": "Priority", "topic": "Module__c", "region": "Region__c"},
      "value_maps": {"Priority": {"critical": "High", "high": "High", "normal": "Medium", "low": "Low"}},
      "append": {"Description": "summary"}
    }'::jsonb)
on conflict (node_id) do nothing;

-- drop the direct classify -> draft edge (if still present)
delete from flow_edges
where flow_id = '11111111-1111-1111-1111-111111111111'
  and source_node_id = 'e4f01f03-cbf2-5f1a-8e0f-ad83ac8e11c5'
  and target_node_id = '2e18fa56-a635-530b-a5be-7b91eb6ba683';

-- insert the two new edges, idempotently
insert into flow_edges (source_node_id, target_node_id, flow_id, condition)
select v.src, v.tgt, '11111111-1111-1111-1111-111111111111', '{}'::jsonb
from (values
  ('e4f01f03-cbf2-5f1a-8e0f-ad83ac8e11c5'::uuid, '3b9a1f2c-5d6e-4f70-8a1b-000000000008'::uuid),
  ('3b9a1f2c-5d6e-4f70-8a1b-000000000008'::uuid, '2e18fa56-a635-530b-a5be-7b91eb6ba683'::uuid)
) as v(src, tgt)
where not exists (
  select 1 from flow_edges e
  where e.flow_id = '11111111-1111-1111-1111-111111111111'
    and e.source_node_id = v.src
    and e.target_node_id = v.tgt
);
