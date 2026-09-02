-- P7d (FR-48): self-serve workspaces.
--
-- Until now a "workspace" was just a bare tenant_id shared via tenant_members,
-- created by hand. This gives it a name and a home so a new user can create
-- one from the UI (`POST /api/tenants`) instead of waiting for an invite.

create table if not exists tenants (
  tenant_id  uuid primary key default gen_random_uuid(),
  name       text not null,
  created_by uuid,
  created_at timestamptz not null default now()
);

alter table tenants enable row level security;
do $$
begin
  if not exists (select 1 from pg_policy where polname = 'tenants_member_read'
                 and polrelid = 'public.tenants'::regclass) then
    create policy tenants_member_read on tenants
      for select to authenticated
      using (tenant_id in (select tenant_id from tenant_members where user_id = auth.uid()));
  end if;
end $$;

comment on table tenants is
  'P7d: named workspaces. A row is created (with the caller as owner in '
  'tenant_members) by POST /api/tenants.';
