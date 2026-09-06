-- KB source connectors (docs/KB_SOURCE_CONNECTORS.md): make a "connected
-- feed" first-class.
--
-- Connectors #1 (sitemap crawl) and #2 (Google Sheets) each shipped as a
-- one-off: a bespoke api/worker.py handler, a bespoke
-- POST /api/kb/collections/{sid}/{crawl,gsheet,gdoc} endpoint, and a prompt()
-- button in KnowledgeView.tsx. A feed wasn't an object -- it was an array
-- buried in `sources.config` (`crawl_urls`, `gsheets`) plus loose
-- `kb_entries.origin` rows, with no status / last-sync / doc-count / re-sync
-- / disconnect. This table + the KBConnectorSpec registry
-- (interpreter/kb_connectors.py) + one generic sync driver
-- (api/worker.py::_sync_kb_connection) replace that: adding Linear / Discourse
-- / Nolt becomes "register a spec + a sync() generator", no new schema.
--
-- Retrieval is unchanged: `kb_entries.source_id` still points at the parent
-- `internal_kb` collection, so `resolve_sources()` / `p_source_ids` scoping
-- works exactly as before. `connection_id` is only provenance + grouped
-- re-sync/archive.
--
-- `connector` has NO CHECK constraint on purpose -- connectors are data, same
-- rule CLAUDE.md already applies to `flow_nodes.type` and
-- `tenants.case_connector` (migration 084).

create table if not exists kb_source_connections (
  connection_id  uuid primary key default gen_random_uuid(),
  source_id      uuid not null references sources(source_id),  -- the internal_kb collection it feeds
  tenant_id      uuid not null,
  connector      text not null,                    -- KBConnectorSpec slug: public_url|gdocs|gsheets|linear|discourse|nolt
  label          text not null default '',         -- human label; defaults to the URL / sheet id
  config         jsonb not null default '{}'::jsonb,-- {url,max_pages} | {sheet_id,sheet_name} | {doc_id,doc_url}
  watermark      jsonb,                             -- incremental-sync cursor (mirrors graph_sync_state)
  status         text not null default 'active',    -- active | paused | error | archived (soft-delete)
  last_synced_at timestamptz,
  last_result    jsonb,                             -- {entries,unchanged,archived,error} -- for the UI
  created_by     uuid,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

create index if not exists idx_kb_source_connections_source
  on kb_source_connections (source_id) where status <> 'archived';
create index if not exists idx_kb_source_connections_active
  on kb_source_connections (status) where status = 'active';

alter table kb_source_connections enable row level security;
do $$ begin
  if not exists (select 1 from pg_policy where polname = 'kb_source_connections_read'
                 and polrelid = 'public.kb_source_connections'::regclass) then
    create policy kb_source_connections_read on kb_source_connections for select to authenticated
      using (public.is_tenant_member(tenant_id));
  end if;
  if not exists (select 1 from pg_policy where polname = 'kb_source_connections_write'
                 and polrelid = 'public.kb_source_connections'::regclass) then
    create policy kb_source_connections_write on kb_source_connections for all to authenticated
      using (public.is_tenant_editor(tenant_id))
      with check (public.is_tenant_editor(tenant_id));
  end if;
end $$;

comment on table kb_source_connections is
  'KB source connectors (docs/KB_SOURCE_CONNECTORS.md): one row per connected '
  'feed (a crawl root, a Google Sheet/Doc, later Linear/Discourse/Nolt) that '
  'produces kb_entries for its parent internal_kb collection. `connector` is a '
  'KBConnectorSpec slug -- no CHECK, connectors are data.';

-- kb_entries gains provenance: which connection produced this row, and a
-- stable key within that connection (page title for a crawl -- parity with
-- _crawl_site's existing existing_by_title identity; row number for a sheet;
-- doc id for a gdoc). Nullable: manual / upload / review entries have none.
alter table kb_entries
  add column if not exists connection_id uuid,
  add column if not exists external_id   text;

do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'kb_entries_connection_fk') then
    alter table kb_entries add constraint kb_entries_connection_fk
      foreign key (connection_id) references kb_source_connections(connection_id);
  end if;
end $$;

create index if not exists idx_kb_entries_connection
  on kb_entries (connection_id, external_id) where connection_id is not null;

-- ---- backfill: turn the pre-existing loose feeds into connection rows -----
-- `sources.config.crawl_urls[]` -> one public_url connection each
insert into kb_source_connections (source_id, tenant_id, connector, label, config)
select s.source_id, s.tenant_id, 'public_url', u.url,
       jsonb_build_object('url', u.url, 'max_pages', 20)
from sources s
cross join lateral jsonb_array_elements_text(coalesce(s.config->'crawl_urls', '[]'::jsonb)) as u(url)
where s.kind = 'internal_kb';

-- `sources.config.gsheets[]` ({sheet_id, sheet_name}) -> one gsheets connection each
insert into kb_source_connections (source_id, tenant_id, connector, label, config)
select s.source_id, s.tenant_id, 'gsheets',
       coalesce(g.elem->>'sheet_id', ''),
       jsonb_build_object('sheet_id', g.elem->>'sheet_id', 'sheet_name', g.elem->>'sheet_name')
from sources s
cross join lateral jsonb_array_elements(coalesce(s.config->'gsheets', '[]'::jsonb)) as g(elem)
where s.kind = 'internal_kb';

-- gdoc-origin entries -> one gdocs connection per distinct linked doc
insert into kb_source_connections (source_id, tenant_id, connector, label, config)
select distinct on (e.source_id, e.gdoc_id)
       e.source_id, e.tenant_id, 'gdocs',
       coalesce(e.gdoc_url, e.gdoc_id),
       jsonb_build_object('doc_id', e.gdoc_id, 'doc_url', e.gdoc_url)
from kb_entries e
where e.origin = 'gdoc' and e.gdoc_id is not null;

-- point existing entries at their new connection + set external_id
update kb_entries e
set external_id = e.gsheet_row::text, connection_id = c.connection_id
from kb_source_connections c
where c.connector = 'gsheets' and c.source_id = e.source_id
  and c.config->>'sheet_id' = e.gsheet_id and e.origin = 'gsheet';

update kb_entries e
set external_id = e.gdoc_id, connection_id = c.connection_id
from kb_source_connections c
where c.connector = 'gdocs' and c.source_id = e.source_id
  and c.config->>'doc_id' = e.gdoc_id and e.origin = 'gdoc';

-- crawl entries: external_id = title (parity with _crawl_site); connection =
-- the longest-prefix public_url match on the page URL embedded as
-- `<!-- url -->` in body_md, else the source's first public_url connection.
update kb_entries e
set external_id = e.title,
    connection_id = (
      select c.connection_id from kb_source_connections c
      where c.connector = 'public_url' and c.source_id = e.source_id
        and coalesce(substring(e.body_md from '<!-- (.*?) -->'), '') like c.config->>'url' || '%'
      order by length(c.config->>'url') desc
      limit 1)
where e.origin = 'crawl';

update kb_entries e
set external_id = coalesce(e.external_id, e.title),
    connection_id = (
      select c.connection_id from kb_source_connections c
      where c.connector = 'public_url' and c.source_id = e.source_id
      order by c.created_at
      limit 1)
where e.origin = 'crawl' and e.connection_id is null;
