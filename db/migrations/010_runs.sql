-- Phase 6: persist every interpreter run so it's queryable, not just logged.
--
-- One row per `interpreter.run` / `POST /flows/{id}/run`. The stored `trace`
-- (each node already emits {summary, data}) plus `gate` + `retrieval` is the
-- "why did the bot respond this way" record. Tenant-scoped RLS mirrors the
-- flow tables (002); writes are service-role (the interpreter records its
-- own runs), reads via an auth'd client are tenant-scoped.

create table if not exists runs (
  run_id       uuid primary key default gen_random_uuid(),
  flow_id      uuid not null references flows(flow_id) on delete cascade,
  tenant_id    uuid not null,
  team         text not null,
  source       text not null default 'api',        -- 'api' | 'cli'
  case_id      text,                                -- from the case payload (SF Id / synthetic / null)
  subject      text,
  tier         text,
  region       text,
  outcome      text,                                -- outcome.action
  confidence   numeric,
  gate         jsonb,                               -- confidence_gate dict
  trace        jsonb not null default '[]'::jsonb,
  retrieval    jsonb not null default '[]'::jsonb,  -- [{doc_url, heading_path, rerank_score}]
  sf_writeback jsonb,
  case_payload jsonb,                               -- the input case, for replay / audit
  created_at   timestamptz not null default now()
);

create index if not exists idx_runs_flow    on runs (flow_id, created_at desc);
create index if not exists idx_runs_tenant  on runs (tenant_id, created_at desc);
create index if not exists idx_runs_outcome on runs (tenant_id, outcome);

alter table runs enable row level security;

do $$
begin
  if not exists (select 1 from pg_policy where polname = 'tenant_isolation_runs'
                 and polrelid = 'public.runs'::regclass) then
    create policy tenant_isolation_runs on runs
      for all
      using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()))
      with check (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;
