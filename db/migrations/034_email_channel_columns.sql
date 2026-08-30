-- Phase 20a: columns tenant_integrations needs for the email channel.
--
-- The Salesforce / Slack / Google rows keep using `secret` jsonb as before.
-- The new `kind='email'` row keeps its *non-sensitive* settings in `config`
-- (imap/smtp host+port, username, from address, team, folder, the
-- auto-send master switch) and stores the actual password / OAuth refresh
-- token in Supabase Vault, referenced by `vault_secret_id` (see migration
-- 035). `secret` stays '{}' for the email row.
--
-- Single concern: the poller/worker bookkeeping columns. RLS is unchanged
-- (the table has no policy -> service-role only; the API brokers it after
-- its own tenant-membership / owner check, same as the Slack/Google
-- endpoints).

alter table tenant_integrations
  add column if not exists config          jsonb       not null default '{}'::jsonb,
  add column if not exists vault_secret_id uuid,
  add column if not exists status          text        not null default 'inactive',
  add column if not exists last_poll_at    timestamptz,
  add column if not exists last_error      text,
  add column if not exists cursor          jsonb       not null default '{}'::jsonb,
  add column if not exists updated_by      uuid;

-- 'inactive' = configured but not polled · 'active' = poll it ·
-- 'error' = last poll failed (see last_error).
alter table tenant_integrations
  drop constraint if exists tenant_integrations_status_chk;
alter table tenant_integrations
  add constraint tenant_integrations_status_chk
  check (status in ('inactive', 'active', 'error'));
