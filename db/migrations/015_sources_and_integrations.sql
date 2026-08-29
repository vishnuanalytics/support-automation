-- Phase 12: per-tenant knowledge sources + per-tenant integration creds.
--
-- Until now every tenant/flow retrieved from the same global `doc_chunks`
-- (all Zapier docs) and Salesforce creds came from one `.env`. This adds:
--   * sources          — a named KB source per tenant (or shared)
--   * doc_chunks.source_id / zapier_docs.source_id, backfilled to a shared
--     "zapier-public" source
--   * the retrieval fns take an optional source filter
--   * tenant_integrations — per-tenant SF/Slack/etc credentials

create table if not exists sources (
  source_id  uuid primary key default gen_random_uuid(),
  tenant_id  uuid,                                   -- NULL = shared across tenants
  kind       text not null,                          -- 'zapier_docs' | 'markdown' | 'notion' | ...
  name       text not null,
  config     jsonb not null default '{}'::jsonb,
  status     text not null default 'active',
  created_at timestamptz not null default now(),
  unique (tenant_id, name)
);

-- the shared public-docs source (fixed id so migrations / seeds can reference it)
insert into sources (source_id, tenant_id, kind, name)
values ('50000000-0000-4000-8000-000000000001', null, 'zapier_docs', 'zapier-public')
on conflict (source_id) do nothing;

alter table zapier_docs add column if not exists source_id uuid
  references sources(source_id);
alter table doc_chunks  add column if not exists source_id uuid
  references sources(source_id);
update zapier_docs set source_id = '50000000-0000-4000-8000-000000000001' where source_id is null;
update doc_chunks  set source_id = '50000000-0000-4000-8000-000000000001' where source_id is null;
create index if not exists idx_doc_chunks_source on doc_chunks (source_id);

-- RLS: a tenant sees its own sources + shared ones; docs stay readable to
-- any authenticated user (they're public content, keyed by source now).
alter table sources enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'sources_read'
                 and polrelid = 'public.sources'::regclass) then
    create policy sources_read on sources for select to authenticated
      using (tenant_id is null
             or tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;

-- ---- per-tenant integration credentials --------------------------------
create table if not exists tenant_integrations (
  tenant_id  uuid not null,
  kind       text not null,                          -- 'salesforce' | 'slack' | ...
  secret     jsonb not null,                         -- {SF_USERNAME, SF_CONSUMER_KEY, ...}
  updated_at timestamptz not null default now(),
  primary key (tenant_id, kind)
);
alter table tenant_integrations enable row level security;   -- no policy: service-role only

-- ---- retrieval fns gain an optional source filter ---------------------
-- p_source_ids NULL  -> search everything (unchanged behaviour).
-- Drop the old 2-/6-arg signatures first: a new trailing param makes a
-- *new* overload, and PostgREST then can't disambiguate the old call.
drop function if exists match_doc_chunks(vector, int);
drop function if exists match_doc_chunks_fts(text, int);
drop function if exists match_doc_chunks_hybrid(text, vector, int, int, int, int);

create or replace function match_doc_chunks(
  query_embedding vector(384),
  match_count     int default 20,
  p_source_ids    uuid[] default null
)
returns table (
  chunk_id uuid, doc_url text, chunk_index int, chunk_text text,
  heading_path text, chunk_type text, section text, similarity double precision
)
language sql stable
set search_path = public, pg_temp
as $$
  select c.chunk_id, c.doc_url, c.chunk_index, c.chunk_text,
         c.heading_path, c.chunk_type, c.section,
         1 - (c.embedding <=> query_embedding) as similarity
  from doc_chunks c
  where c.embedding is not null
    and (p_source_ids is null or c.source_id = any(p_source_ids))
  order by c.embedding <=> query_embedding
  limit greatest(match_count, 1);
$$;

create or replace function match_doc_chunks_fts(
  query_text   text,
  match_count  int default 20,
  p_source_ids uuid[] default null
)
returns table (
  chunk_id uuid, doc_url text, chunk_index int, chunk_text text,
  heading_path text, chunk_type text, section text, rank double precision
)
language sql stable
set search_path = public, pg_temp
as $$
  select c.chunk_id, c.doc_url, c.chunk_index, c.chunk_text,
         c.heading_path, c.chunk_type, c.section,
         ts_rank_cd(c.fts, websearch_to_tsquery('english', query_text)) as rank
  from doc_chunks c
  where query_text is not null and query_text <> ''
    and c.fts @@ websearch_to_tsquery('english', query_text)
    and (p_source_ids is null or c.source_id = any(p_source_ids))
  order by rank desc
  limit greatest(match_count, 1);
$$;

create or replace function match_doc_chunks_hybrid(
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
  heading_path text, chunk_type text, section text, score double precision
)
language sql stable
set search_path = public, pg_temp
as $$
  with dense as (
    select c.chunk_id,
           row_number() over (order by c.embedding <=> query_embedding) as r
    from doc_chunks c
    where c.embedding is not null
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
      and (p_source_ids is null or c.source_id = any(p_source_ids))
    limit greatest(sparse_count, 1)
  ),
  fused as (
    select coalesce(d.chunk_id, s.chunk_id) as chunk_id,
           coalesce(1.0 / (rrf_k + d.r), 0.0) + coalesce(1.0 / (rrf_k + s.r), 0.0) as score
    from dense d full outer join sparse s on d.chunk_id = s.chunk_id
  )
  select f.chunk_id, c.doc_url, c.chunk_index, c.chunk_text,
         c.heading_path, c.chunk_type, c.section, f.score
  from fused f join doc_chunks c on c.chunk_id = f.chunk_id
  order by f.score desc
  limit greatest(match_count, 1);
$$;
