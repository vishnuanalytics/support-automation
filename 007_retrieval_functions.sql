-- Phase 2: SQL retrieval functions for the `retrieve` flow node.
--
-- The Phase 1 eval ranked chunks in-process with numpy (fine at 3.5k rows).
-- The interpreter's retrieve node needs server-side ranking so it stays cheap
-- as the corpus grows and so the same primitives back both dense and the
-- lexical (FTS) half of hybrid retrieval.
--
-- Three functions, all STABLE / SECURITY INVOKER:
--   match_doc_chunks         - dense ANN over the HNSW index (005)
--   match_doc_chunks_fts     - sparse, Postgres FTS over doc_chunks.fts (005)
--   match_doc_chunks_hybrid  - RRF fusion of the two, done in SQL
--
-- doc_chunks.chunk_id is uuid (004); the return signatures reflect that.
-- doc_chunks has RLS "select to authenticated using (true)" from 005, so an
-- auth'd caller can read these too; the interpreter uses the service role.

-- ---- dense -------------------------------------------------------------
create or replace function match_doc_chunks(
  query_embedding vector(384),
  match_count     int default 20
)
returns table (
  chunk_id     uuid,
  doc_url      text,
  chunk_index  int,
  chunk_text   text,
  heading_path text,
  chunk_type   text,
  section      text,
  similarity   double precision
)
language sql stable
set search_path = public, pg_temp
as $$
  select c.chunk_id, c.doc_url, c.chunk_index, c.chunk_text,
         c.heading_path, c.chunk_type, c.section,
         1 - (c.embedding <=> query_embedding) as similarity
  from doc_chunks c
  where c.embedding is not null
  order by c.embedding <=> query_embedding
  limit greatest(match_count, 1);
$$;

-- ---- sparse (lexical) ------------------------------------------------------
create or replace function match_doc_chunks_fts(
  query_text  text,
  match_count int default 20
)
returns table (
  chunk_id     uuid,
  doc_url      text,
  chunk_index  int,
  chunk_text   text,
  heading_path text,
  chunk_type   text,
  section      text,
  rank         double precision
)
language sql stable
set search_path = public, pg_temp
as $$
  select c.chunk_id, c.doc_url, c.chunk_index, c.chunk_text,
         c.heading_path, c.chunk_type, c.section,
         ts_rank_cd(c.fts, websearch_to_tsquery('english', query_text)) as rank
  from doc_chunks c
  where query_text is not null
    and query_text <> ''
    and c.fts @@ websearch_to_tsquery('english', query_text)
  order by rank desc
  limit greatest(match_count, 1);
$$;

-- ---- hybrid: reciprocal-rank fusion in SQL -------------------------------
-- score(chunk) = 1/(rrf_k + rank_dense) + 1/(rrf_k + rank_sparse)
-- Missing side contributes 0. rrf_k=60 is the common default (Cormack 2009).
create or replace function match_doc_chunks_hybrid(
  query_text      text,
  query_embedding vector(384),
  match_count     int default 10,
  rrf_k           int default 60,
  dense_count     int default 40,
  sparse_count    int default 40
)
returns table (
  chunk_id     uuid,
  doc_url      text,
  chunk_index  int,
  chunk_text   text,
  heading_path text,
  chunk_type   text,
  section      text,
  score        double precision
)
language sql stable
set search_path = public, pg_temp
as $$
  with dense as (
    select c.chunk_id,
           row_number() over (order by c.embedding <=> query_embedding) as r
    from doc_chunks c
    where c.embedding is not null
    order by c.embedding <=> query_embedding
    limit greatest(dense_count, 1)
  ),
  sparse as (
    select c.chunk_id,
           row_number() over (
             order by ts_rank_cd(c.fts, websearch_to_tsquery('english', query_text)) desc
           ) as r
    from doc_chunks c
    where query_text is not null
      and query_text <> ''
      and c.fts @@ websearch_to_tsquery('english', query_text)
    limit greatest(sparse_count, 1)
  ),
  fused as (
    select coalesce(d.chunk_id, s.chunk_id) as chunk_id,
           coalesce(1.0 / (rrf_k + d.r), 0.0)
         + coalesce(1.0 / (rrf_k + s.r), 0.0) as score
    from dense d
    full outer join sparse s on d.chunk_id = s.chunk_id
  )
  select f.chunk_id, c.doc_url, c.chunk_index, c.chunk_text,
         c.heading_path, c.chunk_type, c.section, f.score
  from fused f
  join doc_chunks c on c.chunk_id = f.chunk_id
  order by f.score desc
  limit greatest(match_count, 1);
$$;
