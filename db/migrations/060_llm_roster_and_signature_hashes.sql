-- Phase 26: keep the free-model fallback chains fresh, and stop wasting
-- vision/OCR calls on email-signature logos.
--
--  * llm_roster — one row per capability ('text' | 'vision' | 'video'); a
--    daily job (scripts/refresh_llm_roster.py) queries OpenRouter's catalog,
--    keeps the models that are $0 *today*, ranks them, and writes them here.
--    `llm.py` reads this (env vars still override). Retired free slugs drop
--    out automatically; new good ones get picked up.
--
--  * signature_hashes — per (tenant, sender domain) md5 of an attached image.
--    After we've seen the same image `seen` >= a threshold it's treated as a
--    signature / logo and skipped before any OCR / vision call.

create table if not exists llm_roster (
  capability   text primary key,               -- text | vision | video
  models       jsonb not null default '[]'::jsonb,   -- free ids, best first
  premium      jsonb not null default '[]'::jsonb,   -- paid fallback ids, best first
  refreshed_at timestamptz not null default now(),
  source       text
);

alter table llm_roster enable row level security;
create policy llm_roster_read on llm_roster for select to authenticated using (true);

-- seed with the current hardcoded defaults so a fresh DB works before the
-- first refresh run.
insert into llm_roster (capability, models, premium, source) values
 ('text',
  '["meta-llama/llama-3.3-70b-instruct:free","deepseek/deepseek-chat-v3-0324:free","google/gemini-2.0-flash-exp:free","qwen/qwen-2.5-72b-instruct:free","mistralai/mistral-small-3.1-24b-instruct:free"]'::jsonb,
  '["google/gemini-2.0-flash-001","openai/gpt-4o-mini","anthropic/claude-3.5-haiku"]'::jsonb,
  'seed'),
 ('vision',
  '["meta-llama/llama-3.2-11b-vision-instruct:free","qwen/qwen2.5-vl-32b-instruct:free","google/gemini-2.0-flash-exp:free"]'::jsonb,
  '["google/gemini-2.0-flash-001","anthropic/claude-3.5-haiku"]'::jsonb,
  'seed'),
 ('video',
  '[]'::jsonb,
  '["google/gemini-2.0-flash-001"]'::jsonb,
  'seed')
on conflict (capability) do nothing;


create table if not exists signature_hashes (
  tenant_id  uuid not null,
  domain     text not null,
  img_hash   text not null,
  seen       int  not null default 1,
  first_seen timestamptz not null default now(),
  last_seen  timestamptz not null default now(),
  primary key (tenant_id, domain, img_hash)
);

create index if not exists signature_hashes_seen_idx on signature_hashes (last_seen);

alter table signature_hashes enable row level security;
create policy signature_hashes_read on signature_hashes for select to authenticated
  using (public.is_tenant_member(tenant_id));
create policy signature_hashes_write on signature_hashes for all to authenticated
  using (public.is_tenant_editor(tenant_id))
  with check (public.is_tenant_editor(tenant_id));
