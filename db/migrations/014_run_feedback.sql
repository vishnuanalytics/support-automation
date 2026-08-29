-- Phase 11: close the loop. After ask_human / handover, capture what the
-- human actually did with the bot's draft.
--
-- A `check_resolution` job (enqueued with a delay when a run ends in
-- ask_human/handover and the Case has an sf_id) reads the Case's latest
-- outbound reply and diffs it against `runs.case_payload`'s draft.

alter table runs
  add column if not exists draft text,                  -- the bot's proposed reply (to diff against)
  add column if not exists human_action text,           -- pending|sent_as_is|edited|rewrote|no_reply
  add column if not exists human_reply text,
  add column if not exists edit_distance numeric,        -- 0 = identical, 1 = unrelated
  add column if not exists feedback_checked_at timestamptz;

create index if not exists idx_runs_feedback
  on runs (tenant_id, human_action) where human_action is not null;
