-- Phase 16: structured policy rules + the queue of internal actions
-- awaiting a human's Slack approval.

-- ---- policy_rules -------------------------------------------------------
create table if not exists policy_rules (
  rule_id    uuid primary key default gen_random_uuid(),
  tenant_id  uuid not null,
  team       text not null,
  name       text not null,
  priority   int  not null default 100,     -- lower = evaluated first
  "when"     jsonb not null default '{}'::jsonb,   -- predicate tree (interpreter/policy.py)
  "then"     jsonb not null default '{}'::jsonb,   -- {type:'route',...} | {type:'task',...}
  status     text not null default 'active',       -- 'active' | 'disabled'
  created_by uuid,
  created_at timestamptz not null default now(),
  updated_by uuid,
  updated_at timestamptz not null default now(),
  unique (tenant_id, team, name)
);

create index if not exists idx_policy_rules_scope
  on policy_rules (tenant_id, team, status);

alter table policy_rules enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'policy_rules_tenant_rw'
                 and polrelid = 'public.policy_rules'::regclass) then
    create policy policy_rules_tenant_rw on policy_rules
      for all to authenticated
      using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()))
      with check (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;

-- ---- action_requests --------------------------------------------------
-- One row per task a `task_dispatch` node raised. It sits `pending` until a
-- human clicks Approve/Reject in Slack; on approval a `create_github_issue`
-- job runs. Unique (run_id, kind) = idempotent per run.
create table if not exists action_requests (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null,
  run_id        uuid,
  rule_name     text,
  kind          text not null,                 -- 'github_issue'
  payload       jsonb not null default '{}'::jsonb,   -- {repo,title,body,labels,assignees}
  status        text not null default 'pending',      -- pending|approved|rejected|expired|done|error
  slack_channel text,
  slack_ts      text,
  decided_by    text,                          -- Slack user id
  decided_at    timestamptz,
  result        jsonb,                         -- {issue_url, number} once done
  error         text,
  created_at    timestamptz not null default now()
);

create unique index if not exists uq_action_requests_run_kind
  on action_requests (run_id, kind) where run_id is not null;
create index if not exists idx_action_requests_status
  on action_requests (status, created_at);

alter table action_requests enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'action_requests_tenant_read'
                 and polrelid = 'public.action_requests'::regclass) then
    -- read-only in the UI; writes are service-role (nodes / Slack callback / worker)
    create policy action_requests_tenant_read on action_requests
      for select to authenticated
      using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;

-- touch triggers
create or replace function touch_policy_rule() returns trigger
language plpgsql as $$
begin new.updated_at = now(); return new; end $$;

drop trigger if exists trg_touch_policy_rule on policy_rules;
create trigger trg_touch_policy_rule before update on policy_rules
  for each row execute function touch_policy_rule();
