-- P6c (FR-47): per-tenant HTTP connections for the `http_request` node.
--
-- A connection is the allow-list: a flow's `http_request` node names a
-- connection `slug`, and the node can only call paths under that connection's
-- `base_url` with its stored `auth`. The secret (token / password) lives in
-- `auth` and is NEVER returned to the browser — RLS grants no `authenticated`
-- policy, so only the service role reads it; the API returns a redacted view.

create table if not exists connections (
  connection_id uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null,
  slug          text not null,
  base_url      text not null,                       -- https://api.vendor.com
  auth          jsonb not null default '{}'::jsonb,  -- {type:'bearer'|'header'|'basic'|'none', ...}
  created_by    uuid,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  unique (tenant_id, slug)
);

alter table connections enable row level security;
-- no `authenticated` policy on purpose: the secret stays server-side. The API
-- (service role) filters to the caller's tenants and strips `auth` before
-- returning.

comment on table connections is
  'P6c: per-tenant HTTP connection (base_url + auth) an `http_request` flow '
  'node calls. Secret in `auth`, service-role read only.';
