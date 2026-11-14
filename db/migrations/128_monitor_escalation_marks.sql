-- Case Monitor: cases a lead says they escalated to an aggregator, and when the
-- aggregator's team responded. Responded only moves the case to the Responded tab;
-- nothing is written to the case system.

create table if not exists monitor_escalation_marks (
  tenant_id    uuid not null,
  case_id      text not null,
  manual       boolean not null default false,  -- added with "I escalated this"
  escalated_at timestamptz,
  escalated_by uuid,
  note         text,
  responded_at timestamptz,
  responded_by uuid,
  primary key (tenant_id, case_id)
);

alter table monitor_escalation_marks enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'monitor_escalation_marks_member_read'
                 and polrelid = 'public.monitor_escalation_marks'::regclass) then
    create policy monitor_escalation_marks_member_read on monitor_escalation_marks
      for select to authenticated
      using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;
