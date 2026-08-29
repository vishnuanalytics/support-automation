-- Phase 14: self-serve internal knowledge base.
--
-- A "collection" is a row in `sources` (Phase 12) with kind='internal_kb'
-- and tenant_id set — so retrieval scoping (`resolve_sources`) and the
-- `p_source_ids` filter on match_doc_chunks* already work unchanged.
-- `kb_entries` is the editable source of truth; on save its markdown is
-- chunked + embedded into the shared `zapier_docs` / `doc_chunks` tables
-- (url 'kb://<source_id>/<entry_id>', source_id set) exactly as
-- ingestion/sources/markdown_source.py already does for the Globex SOP.

-- ---- kb_entries -------------------------------------------------------------
create table if not exists kb_entries (
  entry_id     uuid primary key default gen_random_uuid(),
  source_id    uuid not null references sources(source_id) on delete cascade,
  tenant_id    uuid not null,                       -- denormalised from sources for RLS
  title        text not null,
  body_md      text not null default '',
  status       text not null default 'active',      -- 'active' | 'archived' (soft-delete)
  embed_hash   text,                                -- md5(body_md) at last embed; skip re-embed if unchanged
  chunk_count  int  not null default 0,
  embedded_at  timestamptz,
  created_by   uuid,
  created_at   timestamptz not null default now(),
  updated_by   uuid,
  updated_at   timestamptz not null default now()
);

create index if not exists idx_kb_entries_source on kb_entries (source_id);
create index if not exists idx_kb_entries_tenant on kb_entries (tenant_id, status);

-- ---- RLS -----------------------------------------------------------------
-- kb_entries: a tenant member has full CRUD over their tenant's entries.
alter table kb_entries enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'kb_entries_tenant_rw'
                 and polrelid = 'public.kb_entries'::regclass) then
    create policy kb_entries_tenant_rw on kb_entries
      for all to authenticated
      using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()))
      with check (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;

-- sources: 015 gave `sources_read` (own + shared, select-only). Add write
-- policies, but only for internal_kb collections owned by the caller's
-- tenant — the shared zapier_docs / markdown / gdoc sources stay
-- service-role-managed.
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'sources_insert_internal_kb'
                 and polrelid = 'public.sources'::regclass) then
    create policy sources_insert_internal_kb on sources
      for insert to authenticated
      with check (kind = 'internal_kb'
        and tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
  if not exists (select 1 from pg_policy where polname = 'sources_update_internal_kb'
                 and polrelid = 'public.sources'::regclass) then
    create policy sources_update_internal_kb on sources
      for update to authenticated
      using (kind = 'internal_kb'
        and tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()))
      with check (kind = 'internal_kb'
        and tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
  if not exists (select 1 from pg_policy where polname = 'sources_delete_internal_kb'
                 and polrelid = 'public.sources'::regclass) then
    create policy sources_delete_internal_kb on sources
      for delete to authenticated
      using (kind = 'internal_kb'
        and tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;

-- keep kb_entries.updated_at honest
create or replace function touch_kb_entry() returns trigger
language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists trg_touch_kb_entry on kb_entries;
create trigger trg_touch_kb_entry before update on kb_entries
  for each row execute function touch_kb_entry();
