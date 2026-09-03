-- P9: usage & billing dashboard.
--
-- `runs.trace` already carries a `tokens: {prompt, completion, total}` blob
-- per LLM-calling node (classify / draft / ai_prompt), but nothing rolls it
-- up to the run level — every usage query would otherwise have to unpack
-- `trace` jsonb. `interpreter/runs.py::build_row` now computes these at
-- write time from the trace; `tokens_by_model` keys on the model id so a
-- flow mixing the free Groq default with an opt-in paid Anthropic node can
-- still be priced per-model.

alter table runs
  add column if not exists tokens_total int not null default 0,
  add column if not exists tokens_by_model jsonb not null default '{}'::jsonb;

comment on column runs.tokens_total is
  'Sum of trace[*].data.tokens.total across the run''s LLM-calling nodes.';
comment on column runs.tokens_by_model is
  'tokens_total broken out by trace[*].data.model, e.g. {"openai/gpt-oss-120b": 812}.';
