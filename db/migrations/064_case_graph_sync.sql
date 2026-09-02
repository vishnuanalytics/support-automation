-- KIL-a — checkpoint for the Case-lifecycle -> Neo4j graph sync.
--
-- `ingestion/case_graph_sync.py` walks Salesforce Cases (all statuses, not just
-- closed) and MERGEs one (:Case) + one (:Message) per turn into Neo4j. This
-- table is its resume point: a high-water mark on Case.LastModifiedDate plus
-- running counters, one row per (scope, tenant).
--
-- Infra, not customer data — same treatment as `system_health` (050) and
-- `sf_cdc_state` (043): RLS on, readable by any authenticated user, written
-- only by the service role.

create table if not exists graph_sync_state (
  scope            text primary key,             -- e.g. 'case_graph:00000000-0000-0000-0000-000000000000'
  tenant_id        uuid,
  last_modified    timestamptz,                  -- max Case.LastModifiedDate processed
  cases_synced     bigint not null default 0,
  messages_synced  bigint not null default 0,
  last_run_at      timestamptz,
  updated_at       timestamptz not null default now()
);

alter table graph_sync_state enable row level security;
create policy graph_sync_state_read on graph_sync_state
  for select to authenticated using (true);

comment on table graph_sync_state is
  'KIL-a: resume point (LastModifiedDate high-water mark + counters) for the '
  'Case-lifecycle -> Neo4j graph sync in ingestion/case_graph_sync.py.';
