-- Billing chunk E: per-run breakdown of platform-paid vs. bring-your-own-key
-- token usage, mirroring the existing tokens_by_model/tokens_by_node columns
-- (074/098). {"platform": n, "byok": n} — computed by
-- interpreter/runs.py::_token_usage from each trace entry's new
-- `data.key_source` field (interpreter/registry.py, interpreter/llm.py's
-- last_key_source). Lets interpreter/billing.py exclude BYOK usage from
-- plan-quota/overage calculations, since the platform spent nothing on it.

alter table runs
  add column if not exists tokens_by_key_source jsonb not null default '{}'::jsonb;

comment on column runs.tokens_by_key_source is
  '{"platform": n, "byok": n} token split for this run, from each trace '
  'entry''s data.key_source. A run with no LLM calls (or predating this '
  'column) is {} -- interpreter/billing.py treats missing/unset as '
  'platform for quota purposes, never silently as BYOK.';
