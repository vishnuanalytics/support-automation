"""
Self-serve Salesforce OAuth (2026-09-03) — the "Connect Salesforce"
button, alongside the JWT-bearer path `salesforce.py` already has.
Mirrors `gdrive.py`'s OAuth shape exactly: `authorize_url`/`exchange_code`,
degrading to a clear error when `SF_OAUTH_CLIENT_ID`/`SF_OAUTH_CLIENT_SECRET`
aren't set, so the rest of the app (and CI) runs fine without a
Connected App configured — same as Google/Slack before their own
Connected Apps existed.

Needs a Salesforce Connected App registered once for this platform (not
per customer): Setup → App Manager → New Connected App → enable OAuth →
callback URL = this app's `.../salesforce/oauth/callback` → note the
Consumer Key/Secret as `SF_OAUTH_CLIENT_ID`/`SF_OAUTH_CLIENT_SECRET`.
After that, every tenant just clicks "Connect Salesforce" and approves —
no Salesforce-admin work on their side, unlike the JWT path.

A Salesforce access (session) token is short-lived; unlike Google, there's
no bundled OAuth-credentials helper in `simple_salesforce` — this module
stores `{refresh_token, instance_url}` and mints a fresh access token on
every use via `refresh_access_token`/`client_from_oauth`.
"""

from __future__ import annotations

import os
from typing import Any

_TOKEN_PATH = "/services/oauth2/token"
_AUTH_PATH = "/services/oauth2/authorize"


def available() -> bool:
    return bool(os.environ.get("SF_OAUTH_CLIENT_ID") and os.environ.get("SF_OAUTH_CLIENT_SECRET"))


def _need() -> tuple[str, str]:
    cid, secret = os.environ.get("SF_OAUTH_CLIENT_ID"), os.environ.get("SF_OAUTH_CLIENT_SECRET")
    if not (cid and secret):
        raise RuntimeError(
            "Salesforce OAuth is not configured — set SF_OAUTH_CLIENT_ID / "
            "SF_OAUTH_CLIENT_SECRET in .env (register a Connected App first)"
        )
    return cid, secret


def _login_base(domain: str | None) -> str:
    from .salesforce import _normalize_domain

    d = _normalize_domain(domain)
    if d in ("login", "test"):
        return f"https://{d}.salesforce.com"
    return f"https://{d}.my.salesforce.com"   # a My Domain token, e.g. 'acme-dev-ed.develop.my'


def authorize_url(redirect_uri: str, state: str, *, domain: str | None = None) -> str:
    cid, _ = _need()
    from urllib.parse import urlencode

    q = {
        "response_type": "code", "client_id": cid, "redirect_uri": redirect_uri,
        "state": state, "scope": "api refresh_token",
    }
    return f"{_login_base(domain)}{_AUTH_PATH}?{urlencode(q)}"


def exchange_code(code: str, redirect_uri: str, *, domain: str | None = None) -> dict[str, Any]:
    """code -> {access_token, refresh_token, instance_url, id, ...}."""
    cid, secret = _need()
    import requests

    r = requests.post(f"{_login_base(domain)}{_TOKEN_PATH}", data={
        "grant_type": "authorization_code", "code": code,
        "client_id": cid, "client_secret": secret, "redirect_uri": redirect_uri,
    }, timeout=15)
    r.raise_for_status()
    body = r.json()
    if "refresh_token" not in body:
        raise RuntimeError(
            "Salesforce did not return a refresh_token (the Connected App's OAuth "
            "policy may need 'Perform requests at any time' — check the refresh_token scope)"
        )
    return body


def refresh_access_token(refresh_token: str, instance_url: str) -> str:
    """A stored refresh_token -> a fresh, short-lived access (session) token."""
    cid, secret = _need()
    import requests

    r = requests.post(f"{instance_url.rstrip('/')}{_TOKEN_PATH}", data={
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": cid, "client_secret": secret,
    }, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def client_from_oauth(refresh_token: str, instance_url: str):
    """A ready-to-use `simple_salesforce.Salesforce` client from stored
    OAuth creds — mints a fresh access token first, every call (the
    caller, `client_for`, caches the resulting client for the process
    lifetime, same as the JWT path already does)."""
    import functools

    from simple_salesforce import Salesforce

    access_token = refresh_access_token(refresh_token, instance_url)
    domain = instance_url.split("://", 1)[-1].rstrip("/")
    sf = Salesforce(instance_url=domain, session_id=access_token)
    _to = float(os.environ.get("SF_HTTP_TIMEOUT", "30"))
    sf.session.request = functools.partial(sf.session.request, timeout=_to)
    return sf
