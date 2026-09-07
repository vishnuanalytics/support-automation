-- 101: the `sources` (tenant_id, name) unique constraint ignored `status`.
--
-- A KB collection is soft-deleted (status -> 'archived') by
-- kb_delete_collection, per the "never hard-delete ingested content" rule.
-- But the full unique constraint meant an archived collection permanently
-- reserved its name: recreating a collection with the same name (a very
-- normal thing after a mistaken delete) failed with
--   duplicate key value violates unique constraint "sources_tenant_id_name_key"
--
-- Make it a PARTIAL unique index over live rows only. Two live collections
-- in one tenant still can't share a name; an archived one no longer blocks
-- reuse. As a bonus this also closes the _ensure_org_kb race — two
-- concurrent "Organization knowledge" inserts can no longer both land.

alter table sources drop constraint if exists sources_tenant_id_name_key;

create unique index if not exists sources_tenant_id_name_active_key
  on sources (tenant_id, name)
  where status <> 'archived';
