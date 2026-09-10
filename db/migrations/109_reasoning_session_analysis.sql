-- Response Quality Feedback Loop chunk C: judge whole reasoning-session
-- transcripts (the actual multi-turn "to and fro" between the bot and the
-- responsible agent -- `reasoning_sessions.transcript`/`pointers`, migration
-- 056) -- a real signal that has had zero quality label at all until now,
-- unlike the single (draft, human_reply) pairs chunks A/B already cover.
-- NULL = not yet judged; only judged once the session reaches a terminal
-- state (sent/abandoned), so a session mid-dialogue is never scored on an
-- incomplete transcript.

alter table reasoning_sessions
  add column if not exists session_analysis jsonb;

comment on column reasoning_sessions.session_analysis is
  '{"category": sound|redundant|misguided|abandoned|other|unknown, '
  '"severity": 0.0-1.0 or null, "summary": text} -- interpreter/'
  'session_review.py''s classification of the whole bot<->agent dialogue''s '
  'quality, once the session reaches a terminal state (sent/abandoned). '
  'NULL = not yet judged; sweeps.session_review_sweep works this backlog '
  'down.';
