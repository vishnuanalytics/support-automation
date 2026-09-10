-- Response Quality Feedback Loop, chunk A: stop discarding the "Wrong"
-- verdict's reason. A manager marking a sent reply Wrong today just posts a
-- Slack "coaching" message and the actual reason (if any) is never stored
-- anywhere — the single most valuable label in the KIL review loop, thrown
-- away. This is the first, cheapest step of the plan: make it persistable.
-- Judging/applying that signal is later chunks (B onward) — this one is
-- schema + plumbing only, no new behavior yet.

alter table review_tasks
  add column if not exists reviewer_note text;

comment on column review_tasks.reviewer_note is
  'Optional free-text reason a reviewer attaches when resolving a task '
  '(correct/wrong/dismissed) -- set via the web Review tab today; Slack''s '
  'quick-action buttons stay note-less for speed. Response Quality '
  'Feedback Loop chunk A (2026-09-10).';
