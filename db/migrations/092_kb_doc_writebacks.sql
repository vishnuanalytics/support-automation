-- KB write-back to Google Docs (docs/KB_SOURCE_CONNECTORS.md §2 "Write-back").
--
-- A gdocs `kb_source_connections` row can opt into `config.access =
-- 'write_back'` (default 'read_only'). When the Knowledge Integrity Loop
-- approves a correction for an entry on such a connection
-- (interpreter/kb_writeback.py::apply_kb_change), the bot rewrites the
-- outdated passage in the doc in place, opens a GitHub issue carrying the
-- old->new diff, and drops a Drive comment. A human verifies the applied
-- edit and closes the issue to confirm. The Slack manager review still gates
-- the internal kb_entries copy -- GitHub is *in addition*, not instead.
--
-- This table is the tracking row per doc edit. `pre_edit_markdown` is the
-- full doc snapshot before the edit, kept for the chunk-2 `/revert` path.
-- The `access` / `github_repo` toggle itself lives in
-- `kb_source_connections.config` (jsonb, no column) -- connectors are data.

create table if not exists kb_doc_writebacks (
  id                   uuid primary key default gen_random_uuid(),
  tenant_id            uuid not null,
  connection_id        uuid references kb_source_connections(connection_id),
  entry_id             uuid,                       -- the superseded gdoc-backed entry
  review_task_id       text,
  github_repo          text,
  github_issue_number  int,
  github_issue_url     text,
  status               text not null default 'applied',
                       -- applied | partial | conflict | verified | reverted | error
  blocks               jsonb not null default '[]'::jsonb,   -- [{old, new, applied: bool}]
  pre_edit_markdown    text,
  error                text,
  applied_at           timestamptz not null default now(),
  verified_at          timestamptz
);

create index if not exists idx_kb_doc_writebacks_connection
  on kb_doc_writebacks (connection_id);
create index if not exists idx_kb_doc_writebacks_open
  on kb_doc_writebacks (status) where status in ('applied', 'partial');

alter table kb_doc_writebacks enable row level security;
do $$ begin
  if not exists (select 1 from pg_policy where polname = 'kb_doc_writebacks_read'
                 and polrelid = 'public.kb_doc_writebacks'::regclass) then
    create policy kb_doc_writebacks_read on kb_doc_writebacks for select to authenticated
      using (public.is_tenant_member(tenant_id));
  end if;
  if not exists (select 1 from pg_policy where polname = 'kb_doc_writebacks_write'
                 and polrelid = 'public.kb_doc_writebacks'::regclass) then
    create policy kb_doc_writebacks_write on kb_doc_writebacks for all to authenticated
      using (public.is_tenant_editor(tenant_id))
      with check (public.is_tenant_editor(tenant_id));
  end if;
end $$;

comment on table kb_doc_writebacks is
  'KB write-back (docs/KB_SOURCE_CONNECTORS.md §2): one row per automated '
  'Google-Doc edit made after a KIL correction was approved -- the old->new '
  'blocks, the GitHub issue opened for human verification, and a pre-edit '
  'snapshot for revert.';
