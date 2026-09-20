-- Invite history needs a real "last updated" timestamp for a revoked
-- invite, not just created_at -- revoke_invitation only ever flipped
-- `status` to 'revoked', so there was no way to know *when* that happened
-- (accepted_at already existed for the accepted case; archived_at,
-- migration 113, covers the archived case).

alter table tenant_invitations add column if not exists revoked_at timestamptz;
