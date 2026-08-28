-- Phase 1: Zapier docs ingestion schema
-- Separate from the flow-definition tables (Phase 0) -- this is RAG content.

create extension if not exists vector;

create table if not exists zapier_docs (
  url            text primary key,
  title          text,
  content_hash   text not null,
  raw_text       text not null,
  last_seen_at   timestamptz not null default now(),
  last_changed_at timestamptz not null default now(),
  status         text not null default 'active',   -- 'active' | 'stale' | 'deleted'
  missed_runs    int not null default 0
);

create table if not exists doc_chunks (
  chunk_id     uuid primary key default gen_random_uuid(),
  doc_url      text not null references zapier_docs(url) on delete cascade,
  chunk_index  int not null,
  chunk_text   text not null,
  embedding    vector(384),   -- all-MiniLM-L6-v2 dimension, runs locally/free
  unique (doc_url, chunk_index)
);

create index if not exists idx_zapier_docs_status on zapier_docs(status);
create index if not exists idx_doc_chunks_doc_url on doc_chunks(doc_url);
-- ivfflat needs data present to build well; create after first real ingest run:
-- create index idx_doc_chunks_embedding on doc_chunks using ivfflat (embedding vector_cosine_ops);
