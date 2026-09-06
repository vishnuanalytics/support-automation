-- Follow-up to the DUPLICATE_OF-never-fires fix (see PROJECT_SCOPE.md's
-- "Immediate next step" and the two ingestion/case_memory_sync.py fixes
-- already applied this session): the final blocker. Neo4j's same-account
-- gate (interpreter/case_memory.py's `_MERGE_CYPHER`) compares the case
-- currently being synced against each kNN neighbour's `account_id` — but
-- `case_memory` (048) never had an `account_id` column, and
-- `match_case_memory()` never returned one, so the neighbour side of every
-- comparison was always missing. The two Python-side fixes made
-- `account_id` correct for the *current* row; this closes the loop so a
-- *neighbour* row's `account_id` is actually retrievable too.

alter table case_memory add column if not exists account_id text;

-- match_case_memory()'s return shape is changing (a new column) --
-- CREATE OR REPLACE can't change an existing function's return type, so
-- the old one must be dropped first.
drop function if exists match_case_memory(vector(384), uuid, int);

create or replace function match_case_memory(
  query_embedding vector(384),
  p_tenant        uuid,
  match_count     int default 10
)
returns table (
  case_sf_id      text,
  case_number     text,
  subject         text,
  body_summary    text,
  case_type       text,
  module          text,
  tier            text,
  resolution_kind text,
  resolution_text text,
  generalizable   boolean,
  resolved_at     timestamptz,
  account_id      text,
  similarity      double precision
)
language sql stable
set search_path = public, pg_temp
as $$
  select m.case_sf_id, m.case_number, m.subject, m.body_summary,
         m.case_type, m.module, m.tier, m.resolution_kind, m.resolution_text,
         m.generalizable, m.resolved_at, m.account_id,
         1 - (m.embedding <=> query_embedding) as similarity
  from case_memory m
  where m.embedding is not null
    and m.status = 'active'
    and m.tenant_id = p_tenant
  order by m.embedding <=> query_embedding
  limit greatest(match_count, 1);
$$;
