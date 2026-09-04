"""
2026-09-04 -- one-time backfill: move every still-plaintext
`tenant_integrations.secret` (salesforce / slack / google / llm -- email
was already Vault-backed since Phase 20a) into Supabase Vault, replacing
the row's `secret` with the safe/redacted view the new code now expects.

Idempotent and safe to re-run: a row whose `secret` already looks like a
redacted marker (no real credential keys left) is skipped.

    python -m scripts.backfill_vault_secrets           # do it
    python -m scripts.backfill_vault_secrets --dry-run # show what would change
"""

from __future__ import annotations

import argparse

from dotenv import load_dotenv

load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402
from interpreter import salesforce, vault_secrets  # noqa: E402

# secret keys that mean "this row is still plaintext" per kind -- if a row's
# `secret` has none of its kind's real-credential keys, it's already safe
# (either backfilled already, or genuinely empty) and is left alone.
_REAL_KEYS = {
    "salesforce": ("SF_CONSUMER_KEY", "SF_PRIVATE_KEY", "SF_PASSWORD", "SF_SECURITY_TOKEN",
                  "SF_OAUTH_REFRESH_TOKEN", "SF_CONSUMER_SECRET"),
    "slack": ("bot_token",),
    "google": ("refresh_token",),
    "llm": ("groq_api_key", "anthropic_api_key", "openrouter_api_key"),
    "github": ("token",),
}


def _redacted_row(kind: str, secret: dict) -> dict:
    if kind == "salesforce":
        return salesforce.redact_org_secret(secret)
    if kind == "slack":
        return {"team": secret.get("team"), "has_credentials": True}
    if kind == "github":
        return {"has_credentials": True}
    if kind == "google":
        return {"scope": secret.get("scope"), "has_credentials": True}
    return {}  # llm: nothing safe to keep on display


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scripts.backfill_vault_secrets")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    sb = get_supabase()
    rows = (sb.table("tenant_integrations")
            .select("tenant_id, kind, org_label, secret").neq("kind", "email")
            .execute().data or [])

    migrated = skipped = 0
    for r in rows:
        kind, secret = r["kind"], r.get("secret") or {}
        real_keys = _REAL_KEYS.get(kind, ())
        if not any(k in secret for k in real_keys):
            skipped += 1
            continue

        org_label = r.get("org_label") or "default"
        vault_kind = f"{kind}:{org_label}" if kind == "salesforce" else kind
        redacted = _redacted_row(kind, secret)
        print(f"{'[dry-run] ' if a.dry_run else ''}migrating tenant={r['tenant_id']} "
              f"kind={kind} org={org_label} -> vault kind={vault_kind!r}, "
              f"row becomes {redacted}")
        if a.dry_run:
            migrated += 1
            continue

        vault_id = vault_secrets.put(r["tenant_id"], vault_kind, secret, sb=sb)
        update = {"secret": redacted}
        if vault_id:
            update["vault_secret_id"] = vault_id
        (sb.table("tenant_integrations").update(update)
         .eq("tenant_id", r["tenant_id"]).eq("kind", kind).eq("org_label", org_label)
         .execute())
        migrated += 1

    print(f"done: {migrated} migrated, {skipped} already safe/empty, "
          f"{len(rows)} non-email rows total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
