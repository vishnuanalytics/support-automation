-- KB write-back (docs/KB_SOURCE_CONNECTORS.md §2): a per-tenant *default* for
-- the two independent Google-Doc knobs (`index`, `on_correction`), so an org
-- sets the policy once and every new gdocs connection inherits it (a
-- per-connection value in the "+ add source" form still overrides).
--
-- Data-as-column, same philosophy as `tenants.case_connector` (migration
-- 084) — a small partial blob, `{}` = "use the system defaults"
-- (`index: true`, `on_correction: off`). `api/main.py::_kb_add_connection`
-- merges this under an incoming gdocs config before `_norm_gdocs`.

alter table tenants
  add column if not exists kb_doc_defaults jsonb not null default '{}'::jsonb;

comment on column tenants.kb_doc_defaults is
  'Per-tenant default for a Google-Doc connection''s {index, on_correction, '
  'github_repo} — inherited by a new gdocs kb_source_connections row unless '
  'the "+ add source" form set an explicit value. {} = system defaults.';
