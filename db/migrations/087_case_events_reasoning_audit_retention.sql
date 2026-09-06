-- Follow-up to the 2026-09-05 architecture/storage audit (PROJECT_SCOPE.md,
-- "Immediate next step"): case_events (062), reasoning_sessions (056), and
-- audit_log (076) were the only append-only tables with no prune/retention
-- policy, unlike runs/jobs (purge_old(), 059) and flow_versions
-- (purge_old_flow_versions(), 077).
--
-- case_events and audit_log are pure history — no "still in flight" concept
-- (case_events is explicitly retained *longer* than runs per 062's own
-- comment, so this defaults to a generous 365 days, not runs' 60).
--
-- reasoning_sessions is different: an open session (state not in
-- ('sent','abandoned')) is a live in-progress dialogue, never safe to
-- delete regardless of age — same "never delete the live one" discipline
-- 077 applies to a flow's published_version. Only terminal-state rows are
-- eligible, aged off `updated_at` (when the session actually finished, not
-- when it started).

create or replace function purge_old_case_events(
  retain_days int default 365
) returns table (events_deleted bigint)
language plpgsql as $$
declare n bigint;
begin
  delete from case_events
   where ts < now() - make_interval(days => retain_days);
  get diagnostics n = row_count;
  return query select n;
end $$;

comment on function purge_old_case_events(int) is
  'Age-only prune of case_events (pure history, no in-flight concept). '
  'Default 365d — retained longer than runs (60d) per 062''s own comment. '
  'Called from scripts/purge_old.py alongside the jobs/runs/flow_versions purge.';


create or replace function purge_old_audit_log(
  retain_days int default 365
) returns table (events_deleted bigint)
language plpgsql as $$
declare n bigint;
begin
  delete from audit_log
   where created_at < now() - make_interval(days => retain_days);
  get diagnostics n = row_count;
  return query select n;
end $$;

comment on function purge_old_audit_log(int) is
  'Age-only prune of audit_log (pure history). Default 365d. Called from '
  'scripts/purge_old.py alongside the jobs/runs/flow_versions purge.';


create or replace function purge_old_reasoning_sessions(
  retain_days int default 365
) returns table (sessions_deleted bigint)
language plpgsql as $$
declare n bigint;
begin
  delete from reasoning_sessions
   where state in ('sent', 'abandoned')
     and updated_at < now() - make_interval(days => retain_days);
  get diagnostics n = row_count;
  return query select n;
end $$;

comment on function purge_old_reasoning_sessions(int) is
  'Prune of reasoning_sessions -- only terminal states (sent/abandoned), '
  'aged off updated_at; never touches an open/in-progress session '
  'regardless of age. Default 365d. Called from scripts/purge_old.py '
  'alongside the jobs/runs/flow_versions purge.';
