-- Phase 20a: store integration credentials in Supabase Vault, not plaintext.
--
-- The email channel (Phase 20) collects a mailbox app-password / Gmail OAuth
-- refresh token from the UI. Those must not sit in `tenant_integrations`
-- in the clear. Supabase Vault (`supabase_vault` ext, already installed)
-- encrypts secrets at rest with a project-managed key; `vault` is not a
-- PostgREST-exposed schema, so the API (service_role) reaches it through
-- these SECURITY DEFINER wrappers in `public`.
--
-- One Vault secret per (tenant, kind), named `integration:<tenant>:<kind>`.
-- The plaintext payload is a small JSON string, e.g.
--   {"kind":"imap","password":"..."}  or  {"kind":"gmail","refresh_token":"..."}
--
-- Generic on purpose: a later phase can migrate the Slack / SF / Google
-- rows onto the same mechanism. This migration only adds the functions.

create or replace function public.integration_secret_put(
  p_tenant uuid, p_kind text, p_plaintext text
) returns uuid
language plpgsql
security definer
set search_path = public, vault, pg_temp
as $$
declare
  v_name text := 'integration:' || p_tenant::text || ':' || p_kind;
  v_id   uuid;
begin
  select id into v_id from vault.secrets where name = v_name;
  if v_id is null then
    v_id := vault.create_secret(p_plaintext, v_name,
                                'tenant_integrations credential (' || p_kind || ')');
  else
    perform vault.update_secret(v_id, p_plaintext);
  end if;
  return v_id;
end $$;

create or replace function public.integration_secret_get(
  p_tenant uuid, p_kind text
) returns text
language sql
security definer
set search_path = public, vault, pg_temp
as $$
  select decrypted_secret
  from vault.decrypted_secrets
  where name = 'integration:' || p_tenant::text || ':' || p_kind;
$$;

create or replace function public.integration_secret_delete(
  p_tenant uuid, p_kind text
) returns void
language sql
security definer
set search_path = public, vault, pg_temp
as $$
  delete from vault.secrets
  where name = 'integration:' || p_tenant::text || ':' || p_kind;
$$;

-- these are the API's broker: only the service role (and the owner
-- postgres) may execute them; never anon / authenticated.
revoke all on function public.integration_secret_put(uuid, text, text)  from public, anon, authenticated;
revoke all on function public.integration_secret_get(uuid, text)        from public, anon, authenticated;
revoke all on function public.integration_secret_delete(uuid, text)     from public, anon, authenticated;
grant execute on function public.integration_secret_put(uuid, text, text) to service_role;
grant execute on function public.integration_secret_get(uuid, text)       to service_role;
grant execute on function public.integration_secret_delete(uuid, text)    to service_role;
