-- Team tab: an owner asked to see the full invite history (how many
-- people they've invited, and each one's pending/accepted/revoked status
-- — already tracked in tenant_invitations.status, just never surfaced
-- past the "Pending invites" list) plus a way to archive old accepted/
-- revoked rows out of that history view once they're no longer relevant,
-- without deleting them -- same soft-delete discipline as
-- zapier_docs.status / kb_entries.status elsewhere in this project.

alter table tenant_invitations add column if not exists archived_at timestamptz;

comment on column tenant_invitations.archived_at is
  'Set by POST /api/invitations/{id}/archive (owner-only, non-pending '
  'rows only) to hide a resolved invite from the default history view. '
  'Never cleared -- one-way, same as status=revoked has no "un-revoke".';
