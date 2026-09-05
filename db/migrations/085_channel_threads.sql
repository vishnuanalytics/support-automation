-- Multi-provider connectors, step 3: Freshchat (and any future chat/call
-- channel) needs to reuse the SAME Case across a long-lived conversation,
-- the way email does via Message-ID threading -- but a chat conversation_id
-- isn't an email Message-ID, and which system holds "the Case" is itself
-- now pluggable (step 1: tenants.case_connector). This is a small, generic,
-- channel-agnostic mapping: (tenant, channel, external thread key) -> the
-- case reference the run created, so the next inbound message on the same
-- conversation attaches to the same case instead of creating a new one.
--
-- `case_ref` deliberately isn't named `sf_id` despite matching what
-- `state.case.sf_id` holds in the interpreter (that field name is a
-- pre-multi-provider naming quirk kept for now, not proof this is
-- Salesforce-specific) -- this table is connector-agnostic by design.

create table if not exists channel_threads (
  tenant_id    uuid not null,
  channel      text not null,        -- 'freshchat' | future: 'freshdesk_chat' | ...
  thread_key   text not null,        -- e.g. a Freshchat conversation_id
  case_ref     text not null,        -- whatever the active case connector calls the case
  case_number  text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  primary key (tenant_id, channel, thread_key)
);

create index if not exists channel_threads_case_idx on channel_threads (tenant_id, case_ref);

alter table channel_threads enable row level security;
create policy channel_threads_read on channel_threads for select to authenticated
  using (public.is_tenant_member(tenant_id));
create policy channel_threads_write on channel_threads for all to authenticated
  using (public.is_tenant_editor(tenant_id))
  with check (public.is_tenant_editor(tenant_id));

comment on table channel_threads is
  'Multi-provider connectors step 3 -- external conversation/thread id -> '
  'case reference, so a chat channel (Freshchat, ...) reuses the same case '
  'across a long-lived conversation instead of creating a new one per message.';
