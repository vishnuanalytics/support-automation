-- Scoped from a 2026-09-03 multi-tenant audit: `tenant_integrations` had
-- `primary key (tenant_id, kind)` -- a tenant could have at most ONE
-- `kind='salesforce'` row, ever. Connecting a second org (a sandbox
-- alongside production, or separate business units each on their own
-- org) wasn't just unbuilt, it was schema-blocked.
--
-- Adds `org_label` (default 'default', backward compatible with every
-- existing row and every existing `client_for(tenant_id)` call site --
-- none of them pass a third argument, so they keep resolving the
-- tenant's 'default' org unchanged) and widens the primary key so a
-- tenant can hold N named Salesforce connections. `email`/`google`/
-- `slack` rows keep behaving as one-per-tenant by convention (nothing in
-- the app ever varies `org_label` for those kinds) -- not schema-enforced,
-- matching this table's existing loose `kind text` typing (no enum/check
-- there either).

alter table tenant_integrations add column if not exists org_label text not null default 'default';

alter table tenant_integrations drop constraint if exists tenant_integrations_pkey;
alter table tenant_integrations add primary key (tenant_id, kind, org_label);
