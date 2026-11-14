-- Case Monitor: what kind of reply a third party sent (an acknowledgement, a
-- real update, a request for more details). Labels are kept per message so a
-- sync never pays to classify the same text twice.

create table if not exists monitor_reply_labels (
  tenant_id  uuid not null,
  text_hash  text not null,
  kind       text not null,
  summary    text,
  method     text not null,   -- rule | llm
  created_at timestamptz not null default now(),
  primary key (tenant_id, text_hash)
);

alter table monitor_reply_labels enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'monitor_reply_labels_member_read'
                 and polrelid = 'public.monitor_reply_labels'::regclass) then
    create policy monitor_reply_labels_member_read on monitor_reply_labels
      for select to authenticated
      using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;
