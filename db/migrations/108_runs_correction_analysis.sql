-- Response Quality Feedback Loop chunk B: score the (bot draft, human's
-- actual final reply) pairs chunk A made visible (GET /api/feedback/
-- corrections) but didn't judge. NULL = not yet judged (the sweep's own
-- queue marker); a judged row is never NULL again, even when the verdict
-- is "no meaningful difference" ({"category": "none", ...}) or the judge
-- itself failed ({"category": "unknown", ...}) -- only a real judge call
-- ever writes here, so NULL always means "still in the backlog."

alter table runs
  add column if not exists correction_analysis jsonb;

comment on column runs.correction_analysis is
  '{"category": tone|factual|policy|brevity|none|other|unknown, '
  '"severity": 0.0-1.0 or null, "summary": text} -- interpreter/'
  'correction_review.py''s classification of why a human changed this '
  'run''s draft (human_action edited/rewrote) before sending it. NULL = '
  'not yet judged; sweeps.correction_review_sweep works this backlog down.';
