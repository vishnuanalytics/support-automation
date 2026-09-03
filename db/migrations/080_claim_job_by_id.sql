-- claim_job(p_job_id) -- an overload of claim_job() that targets one
-- specific row instead of the oldest queued/reclaimable one.
--
-- Found via a CI failure: tests/test_queue.py enqueues a job and then
-- calls api.worker.process_one(sb), which claims via the zero-arg
-- claim_job() -- a global FIFO claim across the whole `jobs` table.
-- This Supabase project also has a real, always-on worker container
-- (docker-compose, per CLAUDE.md's Docker runtime note) polling that
-- same table continuously, so it can -- and in CI, did -- claim the
-- test's own job before the test's own process_one() call got to it,
-- failing `assert process_one(sb) is True`. This is a genuine race
-- against production infra, not a code bug the fix-supabase-secret PR
-- introduced.
--
-- Fix: let a caller that already knows its own job_id (as
-- jobs.enqueue()'s return value always does) claim exactly that row,
-- immune to any other consumer of the queue. The original zero-arg
-- claim_job() is untouched -- this is a separate overload (Postgres
-- dispatches by argument list), so the real worker's polling loop
-- (which doesn't know a job_id in advance) behaves exactly as before.

create or replace function public.claim_job(p_job_id uuid)
returns jobs
language plpgsql
security definer
set search_path to 'public', 'pg_temp'
as $function$
declare j jobs;
begin
  select * into j from jobs
   where job_id = p_job_id
     and ((status = 'queued'  and run_after <= now())
       or (status = 'running' and locked_at < now() - interval '10 minutes'
           and attempts < max_attempts))
   for update skip locked
   limit 1;
  if not found then return null; end if;
  update jobs set status = 'running', locked_at = now(),
                  attempts = attempts + 1, updated_at = now()
   where job_id = j.job_id returning * into j;
  return j;
end $function$;
