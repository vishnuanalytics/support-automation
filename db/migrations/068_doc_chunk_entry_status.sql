-- P1b (FR-41): make retrieval provisional-aware.
--
-- A `review_writeback` KB entry (KIL-d) lands `provisional` but its chunks
-- went into `doc_chunks` exactly like an `active` entry's — so an unverified
-- correction was retrieved and cited with equal weight, and a `superseded`
-- entry's stale chunks were only removed if `delete_entry` happened to fire.
--
-- Carry the source entry's status onto the chunk. The match_* functions now
-- drop `superseded` chunks outright and surface `entry_status` so `h_draft`
-- can down-weight / flag `provisional` context.

alter table doc_chunks
  add column if not exists entry_status text not null default 'active';
-- 'active' | 'provisional' (retrievable, flagged) | 'superseded' (excluded)

create index if not exists idx_doc_chunks_entry_status
  on doc_chunks (entry_status) where entry_status <> 'active';

-- ── match_doc_chunks (dense) ──────────────────────────────────────────
drop function if exists match_doc_chunks(vector, int, uuid[]);
create function match_doc_chunks(
  query_embedding vector(384),
  match_count     int default 20,
  p_source_ids    uuid[] default null
)
returns table (
  chunk_id uuid, doc_url text, chunk_index int, chunk_text text,
  heading_path text, chunk_type text, section text, entry_status text,
  similarity double precision
)
language sql stable
set search_path = public, pg_temp
as $$
  select c.chunk_id, c.doc_url, c.chunk_index, c.chunk_text,
         c.heading_path, c.chunk_type, c.section, c.entry_status,
         1 - (c.embedding <=> query_embedding) as similarity
  from doc_chunks c
  where c.embedding is not null
    and c.entry_status <> 'superseded'
    and (p_source_ids is null or c.source_id = any(p_source_ids))
  order by c.embedding <=> query_embedding
  limit greatest(match_count, 1);
$$;

-- ── match_doc_chunks_fts (sparse) ────────────────────────────────────
drop function if exists match_doc_chunks_fts(text, int, uuid[]);
create function match_doc_chunks_fts(
  query_text   text,
  match_count  int default 20,
  p_source_ids uuid[] default null
)
returns table (
  chunk_id uuid, doc_url text, chunk_index int, chunk_text text,
  heading_path text, chunk_type text, section text, entry_status text,
  rank double precision
)
language sql stable
set search_path = public, pg_temp
as $$
  select c.chunk_id, c.doc_url, c.chunk_index, c.chunk_text,
         c.heading_path, c.chunk_type, c.section, c.entry_status,
         ts_rank_cd(c.fts, websearch_to_tsquery('english', query_text)) as rank
  from doc_chunks c
  where query_text is not null and query_text <> ''
    and c.fts @@ websearch_to_tsquery('english', query_text)
    and c.entry_status <> 'superseded'
    and (p_source_ids is null or c.source_id = any(p_source_ids))
  order by rank desc
  limit greatest(match_count, 1);
$$;

-- ── match_doc_chunks_hybrid ─────────────────────────────────────────
drop function if exists match_doc_chunks_hybrid(text, vector, int, int, int, int, uuid[]);
create function match_doc_chunks_hybrid(
  query_text      text,
  query_embedding vector(384),
  match_count     int default 10,
  rrf_k           int default 60,
  dense_count     int default 40,
  sparse_count    int default 40,
  p_source_ids    uuid[] default null
)
returns table (
  chunk_id uuid, doc_url text, chunk_index int, chunk_text text,
  heading_path text, chunk_type text, section text, entry_status text,
  score double precision
)
language sql stable
set search_path = public, pg_temp
as $$
  with dense as (
    select c.chunk_id,
           row_number() over (order by c.embedding <=> query_embedding) as r
    from doc_chunks c
    where c.embedding is not null
      and c.entry_status <> 'superseded'
      and (p_source_ids is null or c.source_id = any(p_source_ids))
    order by c.embedding <=> query_embedding
    limit greatest(dense_count, 1)
  ),
  sparse as (
    select c.chunk_id,
           row_number() over (
             order by ts_rank_cd(c.fts, websearch_to_tsquery('english', query_text)) desc
           ) as r
    from doc_chunks c
    where query_text is not null and query_text <> ''
      and c.fts @@ websearch_to_tsquery('english', query_text)
      and c.entry_status <> 'superseded'
      and (p_source_ids is null or c.source_id = any(p_source_ids))
    limit greatest(sparse_count, 1)
  ),
  fused as (
    select coalesce(d.chunk_id, s.chunk_id) as chunk_id,
           coalesce(1.0 / (rrf_k + d.r), 0.0) + coalesce(1.0 / (rrf_k + s.r), 0.0) as score
    from dense d full outer join sparse s on d.chunk_id = s.chunk_id
  )
  select f.chunk_id, c.doc_url, c.chunk_index, c.chunk_text,
         c.heading_path, c.chunk_type, c.section, c.entry_status, f.score
  from fused f join doc_chunks c on c.chunk_id = f.chunk_id
  order by f.score desc
  limit greatest(match_count, 1);
$$;
