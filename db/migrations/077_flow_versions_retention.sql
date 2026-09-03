-- Phase 28, step 2 of 6: flow_versions retention.
--
-- `flow_versions` (008) is an immutable snapshot per publish, with no
-- prune/retention policy — it grows forever (known debt, PROJECT_SCOPE.md).
-- Unlike `runs` (pure telemetry — purge_old(), migration 059), a
-- flow_versions row is a live rollback target, so this can't be a blind
-- age cutoff: it must never delete the currently published version (even
-- an old one — rollback_flow re-points published_version at an old version
-- number directly, it doesn't re-snapshot), and it keeps the most recent
-- N versions per flow regardless of age.

create or replace function purge_old_flow_versions(
  keep_last int default 20,
  min_age_days int default 90
) returns table (versions_deleted bigint)
language plpgsql as $$
declare n bigint;
begin
  with ranked as (
    select fv.flow_id, fv.version,
           row_number() over (partition by fv.flow_id order by fv.version desc) as rn
    from flow_versions fv
  )
  delete from flow_versions fv
  using ranked r, flows f
  where fv.flow_id = r.flow_id and fv.version = r.version
    and r.rn > keep_last
    and fv.created_at < now() - make_interval(days => min_age_days)
    and fv.flow_id = f.flow_id
    and fv.version <> f.published_version;   -- never delete the live snapshot
  get diagnostics n = row_count;
  return query select n;
end $$;

comment on function purge_old_flow_versions(int, int) is
  'Phase 28 step 2: keeps the last `keep_last` versions per flow and '
  'anything newer than `min_age_days` unconditionally; never deletes the '
  'currently published version regardless of age/rank. Called from '
  'scripts/purge_old.py alongside the jobs/runs purge.';
