-- Phase 24e: the reasoning dialogue no longer walks 4–6 pointers one at a
-- time. It asks the LLM-pruned relevant subset (a basic case → 1–2 questions)
-- **all in one message**, then at most `max_rounds` short follow-ups if a
-- critical point is still open. `cursor` is reused as the round counter.

alter table reasoning_sessions
  add column if not exists max_rounds int not null default 3;
