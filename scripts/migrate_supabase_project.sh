#!/usr/bin/env bash
# Copy this app's Supabase project into a new (empty) project — e.g. to move
# region (2026-11-03: Tokyo ap-northeast-1 -> Mumbai ap-south-1). Supabase
# can't change a project's region in place.
#
#   ./scripts/migrate_supabase_project.sh            # copy into an EMPTY target
#   ./scripts/migrate_supabase_project.sh --reset    # wipe the target's copy first
#                                                    # (re-run at cutover)
#   ./scripts/migrate_supabase_project.sh --verify   # only compare row counts
#
# Reads OLD_DB_URL / NEW_DB_URL from .env.migrate (git-ignored via .env.*):
# each project's *Session pooler* URI (Dashboard -> Connect -> Session pooler,
# port 5432, user postgres.<ref>). Not the direct host: that's IPv6-only.
#
# What it copies, and why each step is shaped the way it is:
#   1. extensions that live in `public` (vector, citext) — pg_dump -n public
#      doesn't emit CREATE EXTENSION, and the tables need the types first
#   2. the public schema WITH privileges — migrations REVOKE the vault
#      broker functions from anon/authenticated; --no-privileges would
#      silently re-open them via Supabase's default grants
#   3. public data + auth.users / auth.identities (password hashes are
#      bcrypt, portable; sessions are not copied — everyone re-logs in)
#   4. Vault: secrets are encrypted with a per-project key, so a dump's
#      ciphertext can't be decrypted by the new project. They're read
#      decrypted from the old one and re-created by name (the app looks
#      them up by name); tenant_integrations.vault_secret_id is remapped.
#      Plaintext only ever sits in a 0600 temp file, deleted on exit, and
#      is never printed.
#   5. row counts compared table by table.
#
# Uses the postgres:17-alpine image for pg_dump/psql (server is PG 17).
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="copy"
case "${1:-}" in
  --reset) MODE="reset" ;;
  --verify) MODE="verify" ;;
  "") ;;
  *) echo "unknown flag: $1" >&2; exit 2 ;;
esac

[ -f .env.migrate ] || { echo "missing .env.migrate (OLD_DB_URL / NEW_DB_URL)" >&2; exit 2; }
set -a; . ./.env.migrate; set +a
: "${OLD_DB_URL:?OLD_DB_URL not set in .env.migrate}"
: "${NEW_DB_URL:?NEW_DB_URL not set in .env.migrate}"
for v in OLD_DB_URL NEW_DB_URL; do
  case "${!v}" in *"[YOUR-PASSWORD]"*) echo "$v still has the [YOUR-PASSWORD] placeholder in .env.migrate" >&2; exit 2 ;; esac
  case "${!v}" in *":6543/"*) echo "$v is the transaction pooler (6543) — use the session pooler (5432)" >&2; exit 2 ;; esac
done

ref_of() { sed -E 's#.*://postgres\.([a-z0-9]+):.*#\1#' <<<"$1"; }
OLD_REF=$(ref_of "$OLD_DB_URL"); NEW_REF=$(ref_of "$NEW_DB_URL")
[ "$OLD_REF" != "$NEW_REF" ] || { echo "OLD and NEW are the same project ($OLD_REF) — refusing" >&2; exit 2; }
echo "old: $OLD_REF   new: $NEW_REF   mode: $MODE"

WORK=$(mktemp -d); chmod 700 "$WORK"
trap 'rm -rf "$WORK"' EXIT

pg() {  # pg <tool> <args...>   (tool = psql | pg_dump), files under /w
  docker run --rm -i --network host -v "$WORK:/w" -e PGCONNECT_TIMEOUT=15 \
    postgres:17-alpine "$@"
}
q() { pg psql "$1" -X -A -t -v ON_ERROR_STOP=1 -c "$2"; }  # q <url> <sql> -> bare value(s)

verify() {
  local tables bad=0
  tables=$(q "$OLD_DB_URL" "select string_agg(quote_ident(relname), ' ' order by relname)
                            from pg_class c join pg_namespace n on n.oid=c.relnamespace
                            where n.nspname='public' and c.relkind in ('r','p')")
  printf "%-40s %10s %10s\n" table old new
  for t in $tables auth.users auth.identities; do
    local tt="$t"; [[ "$t" == *.* ]] || tt="public.$t"
    local o n
    o=$(q "$OLD_DB_URL" "select count(*) from $tt")
    n=$(q "$NEW_DB_URL" "select count(*) from $tt" 2>/dev/null || echo MISSING)
    local mark=""; [ "$o" = "$n" ] || { mark="  <-- differs"; bad=1; }
    printf "%-40s %10s %10s%s\n" "$tt" "$o" "$n" "$mark"
  done
  local vo vn vd
  vo=$(q "$OLD_DB_URL" "select count(*) from vault.secrets")
  vn=$(q "$NEW_DB_URL" "select count(*) from vault.secrets")
  vd=$(q "$NEW_DB_URL" "select count(*) from vault.decrypted_secrets where decrypted_secret is not null")
  printf "%-40s %10s %10s  (decryptable in new: %s)\n" vault.secrets "$vo" "$vn" "$vd"
  [ "$vo" = "$vn" ] && [ "$vn" = "$vd" ] || bad=1
  local dangling
  dangling=$(q "$NEW_DB_URL" "select count(*) from public.tenant_integrations t
                              where t.vault_secret_id is not null
                                and not exists (select 1 from vault.secrets s where s.id=t.vault_secret_id)")
  echo "tenant_integrations rows pointing at a missing vault secret: $dangling"
  [ "$dangling" = "0" ] || bad=1
  [ "$bad" = 0 ] && echo "VERIFY: OK" || { echo "VERIFY: MISMATCH" >&2; return 1; }
}

if [ "$MODE" = "verify" ]; then verify; exit; fi

# ── target must be empty, or explicitly reset ─────────────────────────
n_tables=$(q "$NEW_DB_URL" "select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace
                            where n.nspname='public' and c.relkind in ('r','p')")
if [ "$n_tables" != "0" ]; then
  [ "$MODE" = "reset" ] || { echo "target $NEW_REF already has $n_tables public tables — re-run with --reset to wipe and recopy" >&2; exit 2; }
  echo "→ resetting target $NEW_REF (public schema, auth users, vault secrets)"
  pg psql "$NEW_DB_URL" -X -q -v ON_ERROR_STOP=1 <<'SQL'
drop schema public cascade;
create schema public;
grant usage on schema public to postgres, anon, authenticated, service_role;
grant all on schema public to postgres, service_role;
delete from auth.identities;
delete from auth.users;
delete from vault.secrets;
SQL
fi

echo "→ 1/5 extensions in public"
q "$NEW_DB_URL" "create extension if not exists vector schema public;
                 create extension if not exists citext schema public;" >/dev/null

echo "→ 2/5 schema (public, with privileges)"
pg pg_dump "$OLD_DB_URL" --schema-only --schema=public --no-owner -f /w/schema.sql
# extensions are created above; drop the dump's own extension/schema noise,
# and supabase_admin's default ACLs — platform-owned, already present in
# every new project, and `postgres` isn't allowed to set them
sed -i -E '/^CREATE SCHEMA public;$/d; /^COMMENT ON SCHEMA public /d;
           /^ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin /d' "$WORK/schema.sql"
pg psql "$NEW_DB_URL" -X -q -v ON_ERROR_STOP=1 -f /w/schema.sql

echo "→ 3/5 data (public + auth users/identities)"
pg pg_dump "$OLD_DB_URL" --data-only --schema=public --no-owner -f /w/data_public.sql
pg pg_dump "$OLD_DB_URL" --data-only --table=auth.users --table=auth.identities --no-owner -f /w/data_auth.sql
{ echo "SET session_replication_role = replica;"; cat "$WORK/data_auth.sql" "$WORK/data_public.sql"; } > "$WORK/data.sql"
pg psql "$NEW_DB_URL" -X -q -v ON_ERROR_STOP=1 -f /w/data.sql

echo "→ 4/5 vault secrets (re-created by name; never printed)"
umask 077
pg psql "$OLD_DB_URL" -X -q -v ON_ERROR_STOP=1 \
  -c "\copy (select id, name, description, decrypted_secret from vault.decrypted_secrets) to '/w/vault.tsv'"
pg psql "$NEW_DB_URL" -X -q -v ON_ERROR_STOP=1 <<'SQL'
create temp table _v (old_id uuid, name text, description text, secret text);
\copy _v from '/w/vault.tsv'
create temp table _m as
  select old_id, vault.create_secret(secret, name, coalesce(description, '')) as new_id from _v;
update public.tenant_integrations t set vault_secret_id = m.new_id
  from _m m where t.vault_secret_id = m.old_id;
SQL
rm -f "$WORK/vault.tsv"

echo "→ 5/5 verify"
verify
