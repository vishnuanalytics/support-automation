-- KB source connector #2 (docs/KB_SOURCE_CONNECTORS.md): a KB entry can be
-- one row of a linked Google Sheet, not just hand-authored markdown or a
-- linked Google Doc. A support/FAQ spreadsheet is structured Q&A/tabular
-- data, not prose -- one `kb_entries` row per data row (never the whole
-- sheet as one blob), same reasoning migration 024 already applied to
-- Google Docs (one row per KB entry), extended to a source that produces
-- *many* entries per sync instead of one.
--
-- Same "reuse the existing Google connection" pattern as gdoc: no new
-- OAuth flow, no new tenant_integrations row -- interpreter/gdrive.py's
-- SCOPES just widens to include spreadsheets.readonly (already-connected
-- tenants need to reconnect once for the new scope to take effect).

alter table kb_entries
  add column if not exists gsheet_id       text,
  add column if not exists gsheet_range    text,        -- the tab/sheet name synced (e.g. "FAQ")
  add column if not exists gsheet_row      int,          -- 1-based row number within that tab
  add column if not exists gsheet_modified text;         -- Drive modifiedTime at last sync

create index if not exists idx_kb_entries_gsheet
  on kb_entries (source_id, gsheet_id, gsheet_row) where origin = 'gsheet';
