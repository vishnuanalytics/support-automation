-- Phase 1: RAG-quality metadata for doc chunks + captured doc-to-doc links.
-- All additive -- new nullable columns, one new table, new indexes. No data loss.
-- Follows 004_docs_ingestion_schema.sql.

-- ---- doc_chunks: structure + hybrid-search columns ----------------------
alter table doc_chunks
  add column if not exists heading_path text,                 -- "Build / CLI / Authentication"
  add column if not exists chunk_type   text not null default 'prose',  -- prose | code | table | list
  add column if not exists token_count  int,
  add column if not exists section      text;                 -- top-level docs area, e.g. 'api-reference'

-- lexical half of hybrid retrieval (dense pgvector + sparse FTS, fused later)
alter table doc_chunks
  add column if not exists fts tsvector
  generated always as (to_tsvector('english', coalesce(chunk_text, ''))) stored;

create index if not exists idx_doc_chunks_fts     on doc_chunks using gin (fts);
create index if not exists idx_doc_chunks_section on doc_chunks (section);

-- HNSW ANN index (pgvector >= 0.5; 0.8.2 installed). Cheap to build now while
-- the table is small; supersedes the ivfflat note in 004.
create index if not exists idx_doc_chunks_embedding_hnsw
  on doc_chunks using hnsw (embedding vector_cosine_ops);

-- ---- doc_links: in-content hyperlinks between docs ---------------------
-- Soft-delete pattern mirrors zapier_docs: a link that disappears from a
-- source page is not dropped immediately -- missed_runs climbs, and only
-- 3 consecutive misses flip it to 'deleted'. Feeds (:Doc)-[:LINKS_TO]->(:Doc).
create table if not exists doc_links (
  source_url    text not null references zapier_docs(url) on delete cascade,
  target_url    text not null,                    -- may point at a not-yet-ingested doc
  anchor_text   text,
  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),
  missed_runs   int not null default 0,
  status        text not null default 'active',   -- 'active' | 'stale' | 'deleted'
  primary key (source_url, target_url)
);

create index if not exists idx_doc_links_target on doc_links (target_url);
create index if not exists idx_doc_links_status on doc_links (status);

-- ---- RLS: these three tables hold PUBLIC Zapier docs, not tenant data --
-- 004 left zapier_docs / doc_chunks with RLS enabled but no policies (deny-all
-- to clients; the ingestion job uses the service role and bypasses RLS).
-- Make that explicit: any authenticated user may read (Phase 5/6 UI); writes
-- stay service-role only. Not tenant-scoped, so no tenant_members join here.
alter table doc_links enable row level security;

do $$
begin
  if not exists (select 1 from pg_policy where polname = 'docs_read_authenticated'
                 and polrelid = 'public.zapier_docs'::regclass) then
    create policy docs_read_authenticated on zapier_docs
      for select to authenticated using (true);
  end if;
  if not exists (select 1 from pg_policy where polname = 'chunks_read_authenticated'
                 and polrelid = 'public.doc_chunks'::regclass) then
    create policy chunks_read_authenticated on doc_chunks
      for select to authenticated using (true);
  end if;
  if not exists (select 1 from pg_policy where polname = 'links_read_authenticated'
                 and polrelid = 'public.doc_links'::regclass) then
    create policy links_read_authenticated on doc_links
      for select to authenticated using (true);
  end if;
end $$;
