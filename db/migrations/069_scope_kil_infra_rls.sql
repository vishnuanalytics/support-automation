-- P1c (FR-42): tenant-scope the KIL infra tables.
--
-- `graph_sync_state` (064) and `handoff_watch_state` (067) shipped with a
-- `select ... using (true)` policy — any authenticated user of any tenant
-- could read another tenant's Case ids + sync cursors. Both carry a
-- `tenant_id`; scope reads to `tenant_members` like every other table
-- (service-role writes are unaffected).

drop policy if exists graph_sync_state_read on graph_sync_state;
create policy graph_sync_state_read on graph_sync_state
  for select to authenticated
  using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));

drop policy if exists handoff_watch_state_read on handoff_watch_state;
create policy handoff_watch_state_read on handoff_watch_state
  for select to authenticated
  using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
