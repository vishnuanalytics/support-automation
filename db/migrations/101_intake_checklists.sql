-- Intake checklists — the "what do we need to know to investigate THIS class
-- of problem" spec that drives the `clarify` node's questions.
--
-- Today `clarify` (interpreter/registry.py) free-writes questions from an LLM
-- with no per-topic structure, so a thin report ("images not showing") gets
-- vague follow-ups and nothing lands in a Salesforce field. A checklist row
-- names the concrete signals an issue class needs (channel, outlet id, publish
-- status, error text, a screenshot ...), each with: how to tell it is already
-- answered (`detect`), the question to ask when it is not, and where the
-- answer lands (`lands_in.sf_field` -> written back via the `update_fields`
-- connector; `lands_in.ctx` -> just carried on the run).
--
--   signals: [
--     { "key": "channels",                       -- stable id
--       "label": "Affected delivery channel(s)",  -- human name
--       "question": "Which channels show the problem — Swiggy, Zomato, both?",
--       "required": true,
--       "detect": { "any_of": ["swiggy", "zomato"] },   -- OR {"regex": "..."}  OR {"attachment_type": "image"}
--       "lands_in": { "ctx": "affected_channels" },      -- OR {"sf_field": "Region__c"}
--       "vision": true }                                 -- also look at image attachments for this one
--   ]
--   match: { "module"?, "submodule"?, "case_type"? }  exact (case-insensitive);
--          { "keywords": [..] }  ANY hit in subject+body+topic.
--          All present conditions must hold; the checklist matching the most
--          conditions wins, `priority` breaks ties.
--
-- Tenant-authored configuration (like `flows`), so tenant RLS via
-- `tenant_members`; the worker uses the service-role key and bypasses it.

create table if not exists intake_checklists (
  checklist_id  uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null,
  label         text not null,
  match         jsonb not null default '{}'::jsonb,
  signals       jsonb not null default '[]'::jsonb,
  priority      int   not null default 0,
  enabled       boolean not null default true,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create index if not exists ix_intake_checklists_tenant
  on intake_checklists (tenant_id) where enabled;

alter table intake_checklists enable row level security;

create policy tenant_isolation_intake_checklists on intake_checklists
  for all
  using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()))
  with check (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));

comment on table intake_checklists is
  'Per-issue-type investigation spec consumed by the clarify node '
  '(interpreter/intake.py): required signals, detect rules, questions, and the '
  'Salesforce field each answer lands in. Drives targeted clarifying questions '
  'when a customer report is too thin to act on.';
