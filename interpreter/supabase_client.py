"""
One factory for every Supabase client this codebase builds (api/main.py's
service + per-request RLS clients, ingestion's `get_supabase()`), so the
transport is configured once.

Why (2026-11-03): under concurrent requests the default client raised
`httpx.RemoteProtocolError: Server disconnected` straight out of an API
handler -> a 500 (seen as 3/20 concurrent runs failing in CI). Two causes,
two fixes:

- postgrest-py defaults to **HTTP/2**, which multiplexes every thread's
  requests over one connection. When the server closes it (GOAWAY / idle
  timeout), every in-flight request on it fails together. We use
  **HTTP/1.1** with a normal connection pool: one request per connection,
  so a dropped connection costs at most that one request.
- a pooled keep-alive connection the server already closed fails on reuse.
  `_RetryTransport` retries that **for idempotent methods only** (GET /
  HEAD / OPTIONS). Writes and RPCs (`claim_job` etc. are POSTs) are never
  retried here: the server may already have applied them, and a duplicate
  insert/claim is worse than a clean error. Connection *establishment*
  failures (nothing was sent) are retried for every method by httpx's own
  `retries=`.

Anything still failing surfaces as an `httpx.TransportError`, which
api/main.py turns into a 503 + Retry-After instead of a 500.
"""

from __future__ import annotations

import logging
import time

import httpx
from supabase import Client, ClientOptions, create_client

log = logging.getLogger(__name__)

_IDEMPOTENT = frozenset({"GET", "HEAD", "OPTIONS"})
_RETRYABLE = (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)
READ_RETRIES = 2
_BACKOFF_S = (0.1, 0.4)
# matches postgrest-py's own DEFAULT_POSTGREST_CLIENT_TIMEOUT
_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


class _RetryTransport(httpx.HTTPTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        attempt = 0
        while True:
            try:
                return super().handle_request(request)
            except _RETRYABLE as e:
                if request.method not in _IDEMPOTENT or attempt >= READ_RETRIES:
                    raise
                log.warning("supabase: %s %s dropped (%s) — retry %d/%d",
                            request.method, request.url.path, e.__class__.__name__,
                            attempt + 1, READ_RETRIES)
                time.sleep(_BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)])
                attempt += 1


def _http_client() -> httpx.Client:
    # A fresh httpx.Client per Supabase client, never shared across them:
    # postgrest sends its (per-client, possibly per-user) auth headers on
    # each request, but keeping one http client per Supabase client means
    # nothing tenant- or user-scoped can ever live on a shared object.
    return httpx.Client(
        transport=_RetryTransport(http2=False, retries=2),
        timeout=_TIMEOUT,
        follow_redirects=True,
    )


def make_client(url: str, key: str) -> Client:
    """`supabase.create_client(url, key)` with the hardened transport."""
    return create_client(url, key, options=ClientOptions(httpx_client=_http_client()))
