-- Phase 10: a Postgres-backed job queue + run idempotency.
--
-- Runs move off the request thread: POST /flows/{id}/enqueue (and the
-- Salesforce trigger) insert a `jobs` row; a worker claims it with
-- FOR UPDATE SKIP LOCKED and executes the flow. The interactive
-- POST /flows/{id}/run stays synchronous for the editor's "try a case".
--
-- Idempotency: a run carries an optional idempotency_key (the SF Case Id
-- for trigger-driven runs); a unique index stops duplicate auto-replies /
-- Chatter posts when a Case is redelivered.

create table if not exists jobs (
  job_id      uuid primary key default gen_random_uuid(),
  kind        text not null,                         -- 'run_flow'
  payload     jsonb not null,                        -- {flow_id, case, idempotency_key?}
  dedupe_key  text,                                  -- kind-scoped; NULL = never deduped
  status      text not null default 'queued',        -- queued | running | done | failed
  attempts    int  not null default 0,
  max_attempts int not null default 3,
  run_after   timestamptz not null default now(),
  locked_at   timestamptz,
  result      jsonb,
  error       text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create index if not exists idx_jobs_claimable on jobs (run_after) where status = 'queued';
-- one live job per (kind, dedupe_key): a redelivered Case won't double-enqueue
create unique index if not exists uq_jobs_dedupe
  on jobs (kind, dedupe_key) where dedupe_key is not null and status in ('queued', 'running');

alter table jobs enable row level security;   -- no policy: infra, service-role only

alter table runs add column if not exists idempotency_key text;
create unique index if not exists uq_runs_idempotency
  on runs (flow_id, idempotency_key) where idempotency_key is not null;

-- claim the next due job, atomically
create or replace function claim_job()
returns jobs
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  j jobs;
begin
  select * into j from jobs
  where status = 'queued' and run_after <= now()
  order by run_after
  for update skip locked
  limit 1;

  if not found then
    return null;
  end if;

  update jobs
     set status = 'running', locked_at = now(), attempts = attempts + 1, updated_at = now()
   where job_id = j.job_id
   returning * into j;
  return j;
end $$;
