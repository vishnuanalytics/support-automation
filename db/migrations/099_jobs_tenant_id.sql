-- Per-tenant failed-jobs visibility.
--
-- `jobs` (013) is service-role-only infra with no `tenant_id`, so a
-- tenant's Approvals/Review tab could show a KIL backlog and stuck
-- reasoning but never "3 of your KB syncs failed permanently" — the one
-- signal that most often explains a silently-degraded bot. `sweeps.
-- failed_jobs_sweep` already pages a global Slack channel; this adds the
-- column those failures can be sliced by.
--
-- Nullable on purpose: cross-tenant infra sweeps (queue_sweep,
-- case_graph_sync, ...) have no single owning tenant and keep it NULL.
-- `interpreter/jobs.enqueue` fills it from the payload; the worker
-- back-fills it post-claim once it has resolved the job's context
-- (api/worker.py::_resolve_job_tenant). No RLS policy: the read path is
-- an owner-only API route on the service client, not client PostgREST.

alter table jobs
  add column if not exists tenant_id uuid;

comment on column jobs.tenant_id is
  'The tenant this job belongs to, when it has exactly one. NULL for '
  'cross-tenant infra sweeps. Filled at enqueue from the payload and '
  'back-filled by the worker after it resolves context. Feeds '
  'GET /api/jobs/failures and the failed-jobs count in /api/health/tenant.';

create index if not exists idx_jobs_failed_by_tenant
  on jobs (tenant_id, updated_at desc)
  where status = 'failed';

-- Backfill 1: payloads that already carry a tenant_id (gdoc_writeback,
-- most run_flow variants, kb writeback re-syncs).
update jobs
   set tenant_id = (payload->>'tenant_id')::uuid
 where tenant_id is null
   and payload ? 'tenant_id'
   and payload->>'tenant_id' ~ '^[0-9a-fA-F-]{36}$';

-- Backfill 2: run_flow jobs -> the flow's tenant.
update jobs j
   set tenant_id = f.tenant_id
  from flows f
 where j.tenant_id is null
   and j.kind = 'run_flow'
   and (j.payload->>'flow_id')::uuid = f.flow_id;

-- Backfill 3: kb_sync / gdoc_writeback -> the connection's tenant.
update jobs j
   set tenant_id = k.tenant_id
  from kb_source_connections k
 where j.tenant_id is null
   and j.kind in ('kb_sync', 'gdoc_writeback')
   and (j.payload->>'connection_id')::uuid = k.connection_id;
