-- KIL-d: KB write-back provenance.
--
-- When a manager confirms a human's KB-contradicting statement was correct,
-- an LLM drafts a `kb_entries` change, it goes through the existing
-- `action_requests` Slack approval (kind='kb_change'), and on approval the
-- worker writes it. A new entry lands `provisional` and is promoted to
-- `active` only after it has survived `provisional_until` with no fresh
-- contradiction -- one bad approval can't permanently poison the KB.

alter table kb_entries
  add column if not exists source_review_task  uuid,   -- -> review_tasks.id
  add column if not exists supersedes_entry_id uuid,   -- the entry this one replaces
  add column if not exists approved_by         text,   -- Slack user id of the approver
  add column if not exists provisional_until   timestamptz;

-- status was 'active' | 'archived'; KIL-d adds 'provisional' (retrievable,
-- flagged) and 'superseded' (dropped from retrieval). No CHECK constraint --
-- flow/KB string columns stay open per the repo convention.

comment on column kb_entries.status is
  'active | provisional (KIL-d, retrievable but unconfirmed) | superseded '
  '(KIL-d, replaced -- chunks removed from retrieval) | archived (soft-delete)';

create index if not exists idx_kb_entries_provisional
  on kb_entries (status, provisional_until)
  where status = 'provisional';
