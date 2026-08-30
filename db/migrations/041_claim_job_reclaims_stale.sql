-- Phase 20i: claim_job() also reclaims jobs a crashed worker left 'running'.
--
-- Before: only `status='queued'` rows were claimable, so a worker killed
-- (SIGKILL / OOM / box sleep) mid-job left the row 'running' forever and no
-- worker would ever touch it again. Now a 'running' job whose `locked_at` is
-- older than 10 minutes (and still under max_attempts) is claimable again.
-- Pairs with the worker's own per-job SIGALRM timeout (WORKER_JOB_TIMEOUT,
-- default 120 s) so a healthy worker never leaves one behind.

create or replace function public.claim_job()
returns jobs
language plpgsql
security definer
set search_path to 'public', 'pg_temp'
as $function$
declare j jobs;
begin
  select * into j from jobs
   where (status = 'queued'  and run_after <= now())
      or (status = 'running' and locked_at < now() - interval '10 minutes'
          and attempts < max_attempts)
   order by run_after
   for update skip locked
   limit 1;
  if not found then return null; end if;
  update jobs set status = 'running', locked_at = now(),
                  attempts = attempts + 1, updated_at = now()
   where job_id = j.job_id returning * into j;
  return j;
end $function$;
