-- Phase 21: Case-resolution memory — remember how past Cases were resolved and
-- feed the closest ones into `draft`.
--
--   Supabase  -> the resolution row + its 384-d embedding + pgvector kNN
--                (this file). One row per resolved Case.
--   Neo4j     -> the relationship layer (Case)-[:RESOLVED_BY]->(Reply),
--                -[:ABOUT]->(Module), -[:SIMILAR_TO]->, -[:DUPLICATE_OF]->
--                (interpreter/case_memory.sync_graph, best-effort).
--
-- `interpreter/case_memory.lookup()` runs the kNN here, applies taxonomy /
-- recency / generalisable boosts, and (best-effort) enriches from Neo4j.
-- `ingestion/case_memory_sync.py` populates it from resolved `runs` rows +
-- closed Salesforce Cases.
--
-- generalisable = the resolution text is a reusable pattern (a how-to, a known
-- fix). false = it cites this customer's own data (IDs / timestamps / log
-- lines) -> usable as an investigation hint, never as reply copy. See the
-- Phase 21 "pattern vs proof" note in PROJECT_SCOPE.

create table if not exists case_memory (
  case_sf_id      text primary key,
  tenant_id       uuid not null,
  case_number     text,
  subject         text,
  body_summary    text,                       -- short, redacted — what was asked
  case_type       text,
  module          text,
  submodule       text,
  region          text,
  tier            text,
  resolution_kind text not null default 'agent_reply'
                    check (resolution_kind in (
                      'auto_reply_accepted', 'agent_reply', 'agent_edit_of_bot',
                      'workaround', 'known_issue', 'diagnostic_finding',
                      'no_fix', 'reopened')),
  resolution_text text,                        -- the reply that resolved it (redacted)
  generalizable   boolean not null default true,
  agent_user_id   text,
  resolved_at     timestamptz,
  embedding       vector(384),
  status          text not null default 'active'   -- 'active' | 'stale' | 'deleted'
                    check (status in ('active', 'stale', 'deleted')),
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create index if not exists idx_case_memory_tenant   on case_memory (tenant_id);
create index if not exists idx_case_memory_embedding on case_memory
  using hnsw (embedding vector_cosine_ops);

alter table case_memory enable row level security;
create policy case_memory_read on case_memory for select to authenticated
  using (public.is_tenant_member(tenant_id));
create policy case_memory_write on case_memory for all to authenticated
  using (public.is_tenant_editor(tenant_id))
  with check (public.is_tenant_editor(tenant_id));

-- dense kNN, tenant-scoped, active rows only. Mirrors match_doc_chunks (007).
-- SECURITY DEFINER so the service role and the worker both use one path; the
-- p_tenant arg is the tenant filter (RLS is bypassed by the service key).
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
  similarity      double precision
)
language sql stable
set search_path = public, pg_temp
as $$
  select m.case_sf_id, m.case_number, m.subject, m.body_summary,
         m.case_type, m.module, m.tier, m.resolution_kind, m.resolution_text,
         m.generalizable, m.resolved_at,
         1 - (m.embedding <=> query_embedding) as similarity
  from case_memory m
  where m.embedding is not null
    and m.status = 'active'
    and m.tenant_id = p_tenant
  order by m.embedding <=> query_embedding
  limit greatest(match_count, 1);
$$;
