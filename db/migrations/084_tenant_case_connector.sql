-- Multi-provider connectors, step 1: which connector is "the case system"
-- for a tenant is now data, not a hardcoded literal.
--
-- interpreter/registry.py's case-touching handlers (sf_case, sf_writeback,
-- ask_human, handover, identify, clarify) all called
-- connectors.invoke(tenant_id, "salesforce", <action>, ...) with a literal
-- string -- proving the *plumbing* (FR-47's ConnectorSpec/invoke()) was
-- generic but the *choice* never was. This column is the tenant-level
-- default connectors.resolve_case_connector() falls back to when a node's
-- own config has no `connector` override. Free text, deliberately no CHECK
-- constraint restricting the value -- same "data, not an enum" philosophy
-- CLAUDE.md already applies to flow_nodes.type, since the whole point is a
-- future Zendesk/HubSpot connector slotting in without a schema change.
-- Defaults to 'salesforce' so every existing tenant/flow is unaffected.

alter table tenants
  add column if not exists case_connector text not null default 'salesforce';

comment on column tenants.case_connector is
  'Connector slug (interpreter/connectors.py) case-touching node handlers '
  'invoke by default when a node has no per-node `connector` override in '
  'its own config. No CHECK constraint on purpose -- connectors are data.';
