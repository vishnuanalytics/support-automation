-- Phase 18c: team invitations.
--
-- An owner pre-authorises an email + role. On that person's next sign-in the
-- API (`POST /api/invitations/accept`, called by the web) matches their
-- verified email to any 'pending' row and inserts the `tenant_members` row
-- with the invited role. No email is sent — the invite IS the authorisation.

create extension if not exists citext;

create table if not exists tenant_invitations (
  invite_id   uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null,
  email       citext not null,
  role        text not null default 'viewer' check (role in ('editor', 'viewer')),
  status      text not null default 'pending' check (status in ('pending', 'accepted', 'revoked')),
  invited_by  uuid,
  created_at  timestamptz not null default now(),
  accepted_at timestamptz
);

-- at most one live invite per (tenant, email)
create unique index if not exists uq_tenant_invite_pending
  on tenant_invitations (tenant_id, email) where status = 'pending';
create index if not exists idx_tenant_invite_email
  on tenant_invitations (email) where status = 'pending';

alter table tenant_invitations enable row level security;

-- an owner of the tenant manages its invitations
create policy tenant_invitations_owner_all on tenant_invitations
  for all to authenticated
  using (exists (select 1 from tenant_members m
                 where m.tenant_id = tenant_invitations.tenant_id
                   and m.user_id = auth.uid() and m.role = 'owner'))
  with check (exists (select 1 from tenant_members m
                      where m.tenant_id = tenant_invitations.tenant_id
                        and m.user_id = auth.uid() and m.role = 'owner'));

-- an invitee can read their own pending invites (the "you've been invited" hint)
create policy tenant_invitations_invitee_read on tenant_invitations
  for select to authenticated
  using (status = 'pending'
    and lower(email::text) = lower(coalesce(auth.jwt() ->> 'email', '')));
