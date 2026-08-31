"""
Phase 23 — fail loud on a broken .env at process start instead of failing
mysteriously mid-request. (A `supabase.com`-vs-`.co` typo cost real time.)

    from interpreter.config import validate_env
    validate_env()          # call at the top of worker / api / poller / cdc main()
"""

from __future__ import annotations

import os
import re
import socket
import sys

_SUPA_RE = re.compile(r"^https://[a-z0-9]{16,}\.supabase\.(co|in|red)$")


class ConfigError(RuntimeError):
    pass


def validate_env(*, strict: bool = True) -> list[str]:
    """Return a list of warnings; raise ConfigError on a fatal problem when
    `strict`. Set SKIP_ENV_CHECK=1 to bypass."""
    if os.environ.get("SKIP_ENV_CHECK") == "1":
        return []

    fatal: list[str] = []
    warn: list[str] = []

    url = os.environ.get("SUPABASE_URL", "")
    if not url:
        fatal.append("SUPABASE_URL is not set")
    elif not _SUPA_RE.match(url.rstrip("/")):
        fatal.append(f"SUPABASE_URL={url!r} doesn't look like https://<ref>.supabase.co "
                     "(a .com typo will NXDOMAIN)")
    else:
        host = url.split("://", 1)[1].split("/", 1)[0]
        try:
            socket.getaddrinfo(host, 443)
        except OSError:
            fatal.append(f"SUPABASE_URL host {host} does not resolve")

    if not os.environ.get("SUPABASE_SERVICE_KEY"):
        fatal.append("SUPABASE_SERVICE_KEY is not set")

    # a Salesforce block is optional, but if present the JWT key must be usable
    if os.environ.get("SF_USERNAME") or os.environ.get("SF_CONSUMER_KEY"):
        keyfile = os.environ.get("SF_PRIVATE_KEY_FILE")
        inline = os.environ.get("SF_PRIVATE_KEY")
        if inline and "BEGIN" in inline:
            pass
        elif keyfile and os.path.exists(keyfile):
            pass
        else:
            fatal.append("SF_* creds set but no readable SF_PRIVATE_KEY / SF_PRIVATE_KEY_FILE")

    if not any(os.environ.get(k) for k in
               ("GROQ_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY")):
        warn.append("no LLM key (GROQ_API_KEY / OPENROUTER_API_KEY / ANTHROPIC_API_KEY) "
                    "— the pipeline will run on the deterministic stub only")

    for w in warn:
        print(f"[config] warning: {w}", file=sys.stderr)
    if fatal and strict:
        raise ConfigError("bad configuration:\n  - " + "\n  - ".join(fatal)
                          + "\n(set SKIP_ENV_CHECK=1 to bypass)")
    for f in fatal:
        print(f"[config] ERROR: {f}", file=sys.stderr)
    return warn + fatal
