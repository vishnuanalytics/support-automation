-- P2a (FR-43): a schema-introspection RPC so `scripts/verify_migrations.py`
-- can diff `db/migrations/*.sql` against the live schema over PostgREST — no
-- direct pooler connection (keeps the audit SB-2 "PostgREST only" rule).
--
-- SECURITY DEFINER, no grant to `authenticated` — only the service key can
-- call it. Returns the public schema's tables, columns, routines and indexes.

create or replace function public.introspect_schema()
returns jsonb
language sql
stable
security definer
set search_path = public, pg_catalog
as $$
  select jsonb_build_object(
    'tables', (
      select coalesce(jsonb_agg(table_name order by table_name), '[]'::jsonb)
      from information_schema.tables
      where table_schema = 'public' and table_type = 'BASE TABLE'
    ),
    'columns', (
      select coalesce(jsonb_agg(jsonb_build_object(
               'table', table_name, 'column', column_name) order by table_name, column_name), '[]'::jsonb)
      from information_schema.columns
      where table_schema = 'public'
    ),
    'routines', (
      select coalesce(jsonb_agg(distinct routine_name order by routine_name), '[]'::jsonb)
      from information_schema.routines
      where routine_schema = 'public'
    ),
    'indexes', (
      select coalesce(jsonb_agg(indexname order by indexname), '[]'::jsonb)
      from pg_indexes
      where schemaname = 'public'
    )
  );
$$;

revoke all on function public.introspect_schema() from public, authenticated, anon;
