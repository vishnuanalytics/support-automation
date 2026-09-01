-- Phase 24 ops hardening (audit C3 + C4):
--   * expression indexes for the lookups the trace endpoint + slack_socket
--     ._deliver do on runs.case_payload->>'sf_id' etc. (were seq scans),
--   * a purge function for old finished jobs / old runs, called nightly from
--     the daily-sync workflow (`python -m scripts.purge_old`).

create index if not exists runs_case_payload_sf_id_idx
  on runs ((case_payload ->> 'sf_id'));
create index if not exists runs_case_payload_case_number_idx
  on runs ((case_payload ->> 'case_number'));
create index if not exists runs_case_id_idx        on runs (case_id);
create index if not exists runs_created_at_idx      on runs (created_at);

create index if not exists jobs_payload_run_id_idx
  on jobs ((payload ->> 'run_id'));
create index if not exists jobs_payload_case_sf_id_idx
  on jobs ((payload #>> '{case,sf_id}'));
create index if not exists jobs_status_created_idx  on jobs (status, created_at);


-- Nightly trim. Finished jobs are pure history after a week; runs older than
-- 60 days whose outcome is settled are safe to drop — the accepted ones are
-- already extracted into `case_memory`. Returns (jobs_deleted, runs_deleted).
create or replace function purge_old(
  jobs_days int default 7,
  runs_days int default 60
) returns table (jobs_deleted bigint, runs_deleted bigint)
language plpgsql as $$
declare j bigint; r bigint;
begin
  delete from jobs
   where status in ('done', 'failed', 'dead')
     and created_at < now() - make_interval(days => jobs_days);
  get diagnostics j = row_count;

  delete from runs
   where created_at < now() - make_interval(days => runs_days)
     and coalesce(human_action, '') not in ('pending')
     and coalesce(outcome, '') <> '';
  get diagnostics r = row_count;

  return query select j, r;
end $$;
