-- Phase 24b: the Slack reasoning dialogue between the bot and the responsible
-- agent. One row per escalated Case. The bot walks a bank of 4–6 "pointer
-- questions" with the agent (bot proposes / agent confirms), drafts the
-- customer reply, and only sends on explicit approval.
--
--   awaiting_handoff -> reasoning -> drafting -> awaiting_approval
--                                                  -> sent | abandoned
--
-- The worker + slackbot use the service key and bypass RLS; the policies are
-- for the editor UI (mirrors 045 / 048).

create table if not exists reasoning_sessions (
  session_id      uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null,
  case_id         text not null,
  case_number     text,
  run_id          uuid,
  state           text not null default 'awaiting_handoff',
  agent_sf_id     text,
  agent_slack_id  text,
  slack_channel   text,
  slack_thread_ts text,
  pointers        jsonb not null default '[]'::jsonb,   -- [{q, why, answered, bot_take, agent_note}]
  cursor          int  not null default 0,
  draft           text,
  transcript      jsonb not null default '[]'::jsonb,   -- [{role, text, at}]
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create index if not exists reasoning_sessions_case_idx   on reasoning_sessions (case_id);
create index if not exists reasoning_sessions_state_idx  on reasoning_sessions (state);
create index if not exists reasoning_sessions_thread_idx on reasoning_sessions (slack_thread_ts);
create index if not exists reasoning_sessions_agent_idx  on reasoning_sessions (agent_slack_id, state);
-- one open session per Case
create unique index if not exists reasoning_sessions_one_open
  on reasoning_sessions (case_id)
  where state not in ('sent', 'abandoned');

alter table reasoning_sessions enable row level security;
create policy reasoning_sessions_read on reasoning_sessions for select to authenticated
  using (public.is_tenant_member(tenant_id));
create policy reasoning_sessions_write on reasoning_sessions for all to authenticated
  using (public.is_tenant_editor(tenant_id))
  with check (public.is_tenant_editor(tenant_id));


-- The seed pointer bank, keyed by Case.Type. The engine takes these and asks
-- the LLM for 1–2 case-specific additions so it's always 4–6. Editable here
-- (or later a UI) without a code deploy — not tenant-scoped, it's shared config.
create table if not exists pointer_bank (
  case_type text primary key,
  pointers  jsonb not null
);

alter table pointer_bank enable row level security;
create policy pointer_bank_read on pointer_bank for select to authenticated using (true);

insert into pointer_bank (case_type, pointers) values
 ('Billing', '[
    "What exactly is the customer disputing — an amount, a date, a plan change, or a failed payment?",
    "What does the billing record actually show for the period in question?",
    "Is this a one-off (goodwill/refund) or a recurring config problem (proration, seats, tax)?",
    "Does answering this need the customer''s own invoice/transaction data, or is the policy answer enough?",
    "What is the blast radius if we state the wrong number — one refund, or a trust problem?"
  ]'::jsonb),
 ('Account / Login', '[
    "What is the customer actually locked out of — the app, SSO, MFA, or a specific role/permission?",
    "Have they tried the standard recovery path, and what happened?",
    "Is there any security signal here (unusual location, repeated failures, shared account)?",
    "Can we resolve this without the customer''s account identifiers, or do we need them to confirm identity first?"
  ]'::jsonb),
 ('Bug', '[
    "What is the exact observed behaviour vs. the expected behaviour?",
    "Is it reproducible, and do we have steps / a payload / a timestamp?",
    "Is this a known issue (KB / prior Case), a config mistake, or genuinely new?",
    "Does a real answer need the customer''s own logs/data, or can we explain the mechanism generally?",
    "What''s the risk of telling them ''it''s fixed'' or ''it''s expected'' if we''re wrong?"
  ]'::jsonb),
 ('How-to', '[
    "What is the customer actually trying to accomplish (the goal, not just the step they asked about)?",
    "Does the KB cover this cleanly, or are we inferring?",
    "Are there prerequisites or plan limits that change the answer for this customer?",
    "Is a generic walkthrough enough, or do they need it tailored to their setup?"
  ]'::jsonb),
 ('Feature Request', '[
    "What is the underlying need behind the request?",
    "Is there an existing workaround we can offer today?",
    "Is this already on the roadmap / a known ask, or new?",
    "What should we honestly commit to — nothing, ''logged'', or a timeframe?"
  ]'::jsonb),
 ('Question', '[
    "What is the precise question, and what decision is the customer trying to make with the answer?",
    "Do we actually know this for certain, or are we guessing?",
    "Is the answer customer-specific, or the same for everyone?",
    "What''s the cost of being wrong here?"
  ]'::jsonb),
 ('Other', '[
    "What is the customer really asking for, in one sentence?",
    "What do we know for certain vs. what are we assuming?",
    "Does a good answer need the customer''s own data, or is a general answer enough?",
    "Who owns this if it isn''t us, and what''s the risk of answering wrong?"
  ]'::jsonb)
on conflict (case_type) do nothing;
