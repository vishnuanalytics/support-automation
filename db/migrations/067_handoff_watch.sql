-- KIL-e: the post-handover watcher's per-Case checkpoint.
--
-- `interpreter/handoff_watch.py` keeps watching an escalated Case: on each
-- sweep it runs the KIL-b contradiction judge on any *new* message and checks
-- the pointer bank for still-unanswered critical questions, flagging the
-- manager thread (never the customer). This row is the resume point + the
-- rate-limit / dedup state so the same conflict isn't re-raised every pass.
--
-- Infra, not customer data — RLS like `system_health` (050) / `graph_sync_state`.

create table if not exists handoff_watch_state (
  case_sf_id    text primary key,
  tenant_id     uuid,
  last_seen_ts  timestamptz,            -- newest message ts processed
  flags_sent    int not null default 0, -- capped at HANDOFF_MAX_FLAGS per Case
  seen_sigs     jsonb not null default '[]'::jsonb,   -- contradiction/pointer signatures already flagged
  updated_at    timestamptz not null default now()
);

alter table handoff_watch_state enable row level security;
create policy handoff_watch_state_read on handoff_watch_state
  for select to authenticated using (true);

comment on table handoff_watch_state is
  'KIL-e: resume point + rate-limit/dedup state for the post-handover watcher '
  '(interpreter/handoff_watch.py).';
