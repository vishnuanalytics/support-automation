-- KIL-c: the human-reply review queue.
--
-- After a human's reply is sent to the customer (the Slack reasoning
-- dialogue's delivery, or the `check_resolution` fallback), the KIL-b
-- contradiction judge runs on it. A `contradicts` / `novel` verdict — or a
-- random sample of the clean ones — raises one row here + a Slack card to the
-- routed-team manager usergroup. A manager marks it correct / wrong /
-- dismissed; a `correct` verdict feeds the KIL-d KB write-back.
--
-- Same access shape as `action_requests` (025): service-role writes (the
-- worker / the Slack callback), tenant members read it in the web Review tab.

create table if not exists review_tasks (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null,
  case_sf_id     text,
  case_number    text,
  run_id         uuid,
  kind           text not null default 'human_reply_review',  -- 'human_reply_review' | 'sample'
  trigger        text,                    -- 'contradicts' | 'novel' | 'sample'
  statement      text,                    -- the human's outbound text, redacted
  verdict        jsonb not null default '{}'::jsonb,   -- interpreter.integrity.check() result
  contexts       jsonb not null default '[]'::jsonb,   -- passages judged against: [{ref,text,kind}]
  status         text not null default 'open',         -- open | correct | wrong | dismissed
  reviewer_id    text,                    -- Slack user id
  reviewed_at    timestamptz,
  kb_change_id   uuid,                    -- -> action_requests.id, set by KIL-d on 'correct'
  slack_channel  text,
  slack_ts       text,
  created_at     timestamptz not null default now()
);

-- one review + at most one sample per run. Plain (non-partial) so PostgREST
-- upsert `on_conflict=run_id,kind` works; NULL run_id rows never collide
-- (Postgres treats NULLs as distinct) which is the intended behaviour for a
-- flag raised without a recorded run.
create unique index if not exists uq_review_tasks_run_kind
  on review_tasks (run_id, kind);
create index if not exists idx_review_tasks_open
  on review_tasks (tenant_id, status, created_at);

alter table review_tasks enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'review_tasks_tenant_read'
                 and polrelid = 'public.review_tasks'::regclass) then
    create policy review_tasks_tenant_read on review_tasks
      for select to authenticated
      using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;

comment on table review_tasks is
  'KIL-c: human-reply review queue. A row per flagged (or sampled) agent reply '
  'that the KIL-b judge checked against the KB + case history.';
