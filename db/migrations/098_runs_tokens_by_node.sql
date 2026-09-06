-- Cost & usage analytics: a per-node token breakdown on each run.
--
-- `runs.tokens_by_model` already rolls up every LLM-calling node's
-- `data.tokens.total` from the trace by model, for the billing dashboard.
-- This adds the same rollup keyed by **node type** (draft / classify /
-- judge / ai_prompt / ...) so a tenant can see which step of a flow costs
-- the most, and `usage_summary` can attribute cost per node.
--
-- Computed at record time (`interpreter/runs.py::_token_usage`) from the
-- trace we already store — no extra work per run. Existing rows keep `{}`.

alter table runs
  add column if not exists tokens_by_node jsonb not null default '{}'::jsonb;

comment on column runs.tokens_by_node is
  'Per-node-type token totals for this run (draft / classify / judge / ...), '
  'rolled from the trace like tokens_by_model. Feeds the per-node cost '
  'breakdown in interpreter/billing.usage_summary.';
