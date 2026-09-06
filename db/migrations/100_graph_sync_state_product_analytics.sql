-- Phase 30 chunk 4 — the product-analytics sync's checkpoint columns.
--
-- `graph_sync_state` (migration 064) already holds a per-(scope, tenant)
-- resume point for the Case-lifecycle graph sync. The PostHog rollup sync
-- reuses it under `scope = 'product_analytics:<tenant>'`, with the
-- `last_modified` column carrying its `last_seen_at` high-water mark. It
-- needs two more counters that the KIL-a sync doesn't:
--   * contacts_synced — running total of (:Contact) rows MERGEd
--   * coverage_pct    — the identity-match rate from the LAST run, the
--                       number surfaced on the connector card
--
-- Nullable / default 0 — existing `case_graph:*` rows are unaffected.

alter table graph_sync_state
  add column if not exists contacts_synced bigint not null default 0,
  add column if not exists coverage_pct    numeric;

comment on column graph_sync_state.coverage_pct is
  'Phase 30, scope ''product_analytics:<tenant>'': the % of PostHog persons '
  'in the most recent sync whose Neo4j (:Contact) resolved to a known '
  'Salesforce contact or account (identity_match != ''none''). Shown on the '
  'PostHog connector card so a tenant sees join quality before trusting '
  'account-level product signals.';
