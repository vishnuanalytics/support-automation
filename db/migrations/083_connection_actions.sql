-- FR-47 follow-up: named, reusable actions on a per-tenant HTTP `connection`.
--
-- `connections` (072) is just `{base_url, auth}` — a flow's `http_request`
-- node names a raw method+path in its own config every time. This table lets
-- a tenant save a *named* action once (e.g. "create_ticket" on a "zendesk"
-- connection) and reuse it from any flow via the generic `connector_action`
-- node, exactly like the built-in `salesforce`/`slack` connectors — the
-- "connector is data, not a hardcoded node handler" spec FR-47 called for,
-- now genuinely true for any REST API a tenant wants to wire up.

create table if not exists connection_actions (
  action_id      uuid primary key default gen_random_uuid(),
  connection_id  uuid not null references connections(connection_id) on delete cascade,
  name           text not null,
  method         text not null default 'GET',
  path           text not null,                       -- relative to the connection's base_url;
                                                        -- "{{ dotted.path }}" over the action's own params
  params         jsonb not null default '[]'::jsonb,   -- [{key,label,type,required,options?}] — drives
                                                        -- the web editor's generic connector-action form
  body_template  jsonb,                                -- optional, "{{ }}"-templated over params
  created_at     timestamptz not null default now(),
  unique (connection_id, name)
);

alter table connection_actions enable row level security;
-- No `authenticated` policy, same as `connections` itself: the API (service
-- role) scopes reads/writes to the caller's tenant via the parent connection.

comment on table connection_actions is
  'FR-47: a named, reusable HTTP action on a connection (method/path/params/'
  'body_template), exposed as a connector_action alongside the salesforce/'
  'slack builtins. No secret here -- auth stays on the parent connection.';
