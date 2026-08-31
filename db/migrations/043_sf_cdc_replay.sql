-- Phase 20l: durable replay-position store for the Salesforce CDC subscriber.
--
-- `ingestion.sf_cdc_watch` is a long-lived gRPC client on the Salesforce
-- Pub/Sub API. Every Case / EmailMessage change event carries a `replay_id`;
-- the subscriber stores the last one it processed per topic so a restart
-- resumes exactly where it left off (Salesforce retains events for 72h) --
-- that is what makes the push durable across our downtime, unlike the
-- fire-and-forget Apex callout.
--
-- Single concern: the replay cursor. Infra bookkeeping like `jobs` -- no
-- tenant column, no RLS policy => service-role only. The API never touches
-- this table.

create table if not exists sf_cdc_state (
  topic       text primary key,          -- e.g. '/data/CaseChangeEvent'
  replay_id   text,                      -- opaque hex of the replay bytes; NULL = start from LATEST
  event_count bigint not null default 0, -- processed since first subscribe
  updated_at  timestamptz not null default now()
);

comment on table sf_cdc_state is
  'Phase 20l: last processed Pub/Sub API replay_id per CDC topic (subscriber resume point).';
