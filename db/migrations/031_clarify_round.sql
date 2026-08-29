-- Phase 17d: cross-run clarify loop.
--
-- `runs.clarify_round` records which back-and-forth this run is for a Case
-- (1 on the first `need_info`, +1 each subsequent one for the same Case).
-- The `clarify` node reads the prior max for `case_id` and, once it would
-- exceed `config.max_rounds` (default 2), routes to a human instead of
-- asking the customer again (`outcome.action = 'ask_human'`,
-- reason `clarify_exhausted`).

alter table runs add column if not exists clarify_round int;

comment on column runs.clarify_round is
  '1-based clarify back-and-forth count for this Case (Phase 17d); NULL for non-clarify runs';
