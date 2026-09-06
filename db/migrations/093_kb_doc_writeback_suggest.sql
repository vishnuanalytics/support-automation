-- KB write-back (docs/KB_SOURCE_CONNECTORS.md §2): a gdocs connection's
-- `config.access` gains a third value, `'suggest'` (between `'read_only'` and
-- `'write_back'`): the bot opens a GitHub issue with the old->new diff + a
-- doc link but does NOT edit the doc — a human applies the change and closes
-- the issue, then the mirror re-syncs. This is the recommended mode for a
-- tenant that wants doc corrections without granting the bot write access.
--
-- No table change (`kb_source_connections.config` is jsonb, `access` has no
-- CHECK). `kb_doc_writebacks.status` gains a `'suggested'` value (also no
-- CHECK) — the only schema touch is widening the partial index the
-- watch query (`ingestion/kb_writeback_watch.py`) rides so it stays useful.

drop index if exists idx_kb_doc_writebacks_open;
create index idx_kb_doc_writebacks_open
  on kb_doc_writebacks (status)
  where status in ('suggested', 'applied', 'partial');
