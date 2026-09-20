"""
Error monitoring — Sentry, optional, off unless SENTRY_DSN is set (same
"off by default, on by env var" pattern as everywhere else in this repo:
Groq/Anthropic model routing, the web side's GTM/GA4). One shared
`init_sentry()`, called once at the top of each long-lived process (api,
worker, cdc, poller, slackbot — see docs/DEPLOY.md) so every event is
tagged with which one it came from: these are five separate OS
processes, not one, and a crash in the poller looks identical to a crash
in the API in a bare stack trace otherwise.

Deliberately conservative about what gets sent, since this app processes
real customer support data (case text, emails) that can end up inside a
stack trace's local variables: PII capture is off (`send_default_pii=
False` -- no request bodies, no user IP by default), and performance
tracing is off (`traces_sample_rate=0`) unless explicitly turned up via
env var -- this is "tell me when something breaks," not a broader
telemetry pipeline. If this is ever turned on for a real deployment,
Sentry becomes a new sub-processor and needs a line in the Privacy
Policy's "who we share data with" section (still blocked on the real
entity details needed to make that document valid at all -- see
PROJECT_SCOPE.md).
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("interpreter.observability")

_initialized = False


def init_sentry(component: str) -> None:
    """No-op if SENTRY_DSN isn't set, or if already called in this process
    (each entrypoint calls this once at startup, right after load_dotenv())."""
    global _initialized
    if _initialized:
        return
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return
    try:
        import sentry_sdk
    except ImportError:
        log.warning(
            "SENTRY_DSN is set but sentry-sdk isn't installed -- "
            "add it to requirements.txt and reinstall"
        )
        return
    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "development"),
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0")),
        send_default_pii=False,
    )
    sentry_sdk.set_tag("component", component)
    _initialized = True
