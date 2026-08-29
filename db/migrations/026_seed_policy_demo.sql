-- Phase 16 seed: a structured rule + an extract -> policy_gate ->
-- task_dispatch chain on the Acme offboarding flow.
--
-- Rule (team 'offboarding'): if the customer asks to export data older than
-- 2 years, that's an ops job — raise a GitHub issue, but only after a lead
-- approves it in Slack (#support-leads). Newer exports fall through to the
-- normal draft path.
--
-- Slack + GitHub creds are per-tenant in `tenant_integrations`
-- (kind='slack' / 'github'); without them the dispatch still records an
-- `action_requests` row, it just isn't posted anywhere.

insert into policy_rules (tenant_id, team, name, priority, "when", "then")
values (
  '00000000-0000-0000-0000-000000000000', 'offboarding',
  'Old data export -> GitHub ops ticket', 50,
  '{"field": "entities.report_age_years", "op": "gte", "value": 2}'::jsonb,
  '{"type": "task", "task": "github_issue", "repo": "acme/support-ops",
    "title_tmpl": "Data export request: {{case.subject}}",
    "body_tmpl": "A departing customer wants data older than 2 years exported — needs ops.\n\n---\n{{case.body}}",
    "labels": ["data-export", "ops"],
    "approval": {"slack_channel": "#support-leads"}}'::jsonb
)
on conflict (tenant_id, team, name) do nothing;

-- new nodes on the Acme offboarding flow
insert into flow_nodes (node_id, flow_id, type, label, position_x, position_y, config) values
  ('c3000005-3333-4333-8333-333333333333', 'c3c3c3c3-3333-4333-8333-333333333333',
   'extract', 'Extract entities', 200, 100,
   '{"fields": {"report_age_years": "how old, in years, are the reports/data the customer wants exported; null if not stated"}}'::jsonb),
  ('c3000006-3333-4333-8333-333333333333', 'c3c3c3c3-3333-4333-8333-333333333333',
   'policy_gate', 'Policy gate', 300, 100, '{}'::jsonb),
  ('c3000007-3333-4333-8333-333333333333', 'c3c3c3c3-3333-4333-8333-333333333333',
   'task_dispatch', 'Dispatch ops task', 400, 40, '{}'::jsonb)
on conflict (node_id) do nothing;

-- classify -> extract  (was classify -> draft)
update flow_edges
set target_node_id = 'c3000005-3333-4333-8333-333333333333'
where edge_id = '8c1ed3ed-160a-4528-b41c-06a1d2129c5c';

insert into flow_edges (edge_id, flow_id, source_node_id, target_node_id, condition) values
  (gen_random_uuid(), 'c3c3c3c3-3333-4333-8333-333333333333',
   'c3000005-3333-4333-8333-333333333333', 'c3000006-3333-4333-8333-333333333333', '{}'::jsonb),
  (gen_random_uuid(), 'c3c3c3c3-3333-4333-8333-333333333333',
   'c3000006-3333-4333-8333-333333333333', 'c3000007-3333-4333-8333-333333333333',
   '{"if": "policy.task"}'::jsonb),
  (gen_random_uuid(), 'c3c3c3c3-3333-4333-8333-333333333333',
   'c3000006-3333-4333-8333-333333333333', 'c3000003-3333-4333-8333-333333333333', '{}'::jsonb),
  (gen_random_uuid(), 'c3c3c3c3-3333-4333-8333-333333333333',
   'c3000007-3333-4333-8333-333333333333', 'c3000004-3333-4333-8333-333333333333', '{}'::jsonb);

-- re-snapshot published flows
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
       md5(f.flow_id::text || '-026')
from flows f
where f.status = 'published';

update flows f
set published_version = (select max(version) from flow_versions v where v.flow_id = f.flow_id),
    version = version + 1
where f.status = 'published';
