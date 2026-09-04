"""
Thin wrapper over the Supabase Vault broker functions (migration `035`,
Phase 20a) so every secret-holding integration -- Salesforce, Slack,
Google, a tenant's own pasted LLM keys -- stores its real credential blob
encrypted in Vault, never as plaintext in `tenant_integrations.secret`.
`interpreter/mailbox.py` already does this inline for the email channel;
this generalizes the same 3 calls for every other kind.

One Vault secret per (tenant, kind). `kind` is just a string the SQL
functions concatenate into the secret's name (`integration:<tenant>:<kind>`)
-- Salesforce is per-org, so it namespaces its own kind as
`f"salesforce:{org_label}"` to give each connected org an independent entry
under `tenant_integrations`' `(tenant_id, kind, org_label)` primary key
(migration `082`); every other kind is one-per-tenant by convention.

    vault_secrets.put(tenant_id, "slack", {"bot_token": "..."})
    vault_secrets.get(tenant_id, "slack")   -> {"bot_token": "..."} or {}
    vault_secrets.delete(tenant_id, "slack")

`tenant_integrations.secret` should hold only non-sensitive display fields
(username, domain, workspace name, a `has_credentials` flag) plus whatever
`vault_secret_id` `put()` returns -- never the real value.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("interpreter.vault_secrets")


def _sb():
    from ingestion.scraper import get_supabase
    return get_supabase()


def get(tenant_id: str | None, kind: str, *, sb=None) -> dict[str, Any]:
    """The real credential dict for `(tenant_id, kind)`, or `{}` if nothing's
    stored (never configured, or deleted) -- never raises."""
    if not tenant_id:
        return {}
    try:
        raw = (sb or _sb()).rpc(
            "integration_secret_get", {"p_tenant": tenant_id, "p_kind": kind}
        ).execute().data
        return json.loads(raw) if raw else {}
    except Exception as e:  # noqa: BLE001
        log.warning("vault_secrets.get(%s, %s): %s", tenant_id, kind, e)
        return {}


def put(tenant_id: str, kind: str, creds: dict[str, Any], *, sb=None) -> str | None:
    """Encrypt + store `creds` as this tenant/kind's Vault secret (replacing
    any prior value). Returns the `vault_secret_id` for the caller to stamp
    on the `tenant_integrations` row, or `None` on failure (best-effort --
    the caller decides whether a failed encrypt should block the save)."""
    try:
        return (sb or _sb()).rpc("integration_secret_put", {
            "p_tenant": tenant_id, "p_kind": kind, "p_plaintext": json.dumps(creds),
        }).execute().data
    except Exception as e:  # noqa: BLE001
        log.warning("vault_secrets.put(%s, %s): %s", tenant_id, kind, e)
        return None


def delete(tenant_id: str, kind: str, *, sb=None) -> None:
    try:
        (sb or _sb()).rpc(
            "integration_secret_delete", {"p_tenant": tenant_id, "p_kind": kind}
        ).execute()
    except Exception as e:  # noqa: BLE001
        log.warning("vault_secrets.delete(%s, %s): %s", tenant_id, kind, e)
