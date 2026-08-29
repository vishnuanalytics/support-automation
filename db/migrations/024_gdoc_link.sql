-- Phase 15: a KB entry can be a *linked Google Doc* rather than
-- hand-authored markdown. It lives in the same `internal_kb` collection as
-- manual entries (so a `kb_lookup` node picks up both), but its body is
-- synced from Drive, not editable in the UI.
--
-- Google OAuth creds reuse `tenant_integrations` (Phase 12): one row per
-- tenant, kind='google', secret = {refresh_token, ...}. No schema change
-- there — it's already service-role-only.

alter table kb_entries
  add column if not exists origin        text not null default 'manual',  -- 'manual' | 'gdoc'
  add column if not exists gdoc_id        text,
  add column if not exists gdoc_url       text,
  add column if not exists gdoc_modified  text,        -- Drive modifiedTime at last sync
  add column if not exists synced_at      timestamptz,
  add column if not exists sync_error     text;

create index if not exists idx_kb_entries_gdoc
  on kb_entries (origin) where origin = 'gdoc';
