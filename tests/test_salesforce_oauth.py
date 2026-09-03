"""
2026-09-03 -- the self-serve "Connect Salesforce" OAuth flow, alongside
the JWT-bearer path. Mirrors `gdrive.py`'s OAuth shape; degrades to a
clear RuntimeError when SF_OAUTH_CLIENT_ID/SECRET aren't set, same as
Google before its own Connected App existed.

Run:  pytest tests/test_salesforce_oauth.py
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import salesforce, salesforce_oauth


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.delenv("SF_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("SF_OAUTH_CLIENT_SECRET", raising=False)
    yield


def test_available_false_without_creds():
    assert salesforce_oauth.available() is False


def test_authorize_url_raises_without_creds():
    with pytest.raises(RuntimeError, match="not configured"):
        salesforce_oauth.authorize_url("http://localhost/cb", "state123")


def test_authorize_url_shape(monkeypatch):
    monkeypatch.setenv("SF_OAUTH_CLIENT_ID", "cid123")
    monkeypatch.setenv("SF_OAUTH_CLIENT_SECRET", "secret")
    url = salesforce_oauth.authorize_url("http://localhost:8000/cb", "nonce-abc")
    assert url.startswith("https://login.salesforce.com/services/oauth2/authorize?")
    assert "client_id=cid123" in url
    assert "state=nonce-abc" in url
    assert "redirect_uri=http" in url
    assert "scope=api" in url


def test_authorize_url_respects_a_sandbox_domain(monkeypatch):
    monkeypatch.setenv("SF_OAUTH_CLIENT_ID", "cid123")
    monkeypatch.setenv("SF_OAUTH_CLIENT_SECRET", "secret")
    url = salesforce_oauth.authorize_url("http://localhost/cb", "n", domain="test")
    assert url.startswith("https://test.salesforce.com/")


def test_authorize_url_respects_a_my_domain(monkeypatch):
    monkeypatch.setenv("SF_OAUTH_CLIENT_ID", "cid123")
    monkeypatch.setenv("SF_OAUTH_CLIENT_SECRET", "secret")
    url = salesforce_oauth.authorize_url("http://localhost/cb", "n", domain="acme-dev-ed.develop.my")
    assert url.startswith("https://acme-dev-ed.develop.my.my.salesforce.com/")


class _FakeResp:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


def test_exchange_code_posts_to_the_right_endpoint_and_returns_the_body(monkeypatch):
    monkeypatch.setenv("SF_OAUTH_CLIENT_ID", "cid123")
    monkeypatch.setenv("SF_OAUTH_CLIENT_SECRET", "sec456")
    captured = {}

    def fake_post(url, data, timeout):
        captured["url"] = url
        captured["data"] = data
        return _FakeResp({"access_token": "at", "refresh_token": "rt",
                          "instance_url": "https://acme.my.salesforce.com"})

    import requests
    monkeypatch.setattr(requests, "post", fake_post)

    body = salesforce_oauth.exchange_code("the-code", "http://localhost/cb")
    assert captured["url"] == "https://login.salesforce.com/services/oauth2/token"
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["code"] == "the-code"
    assert captured["data"]["client_id"] == "cid123"
    assert captured["data"]["client_secret"] == "sec456"
    assert body["refresh_token"] == "rt"


def test_exchange_code_without_a_refresh_token_raises(monkeypatch):
    monkeypatch.setenv("SF_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("SF_OAUTH_CLIENT_SECRET", "sec")
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResp({"access_token": "at"}))
    with pytest.raises(RuntimeError, match="refresh_token"):
        salesforce_oauth.exchange_code("code", "http://localhost/cb")


def test_refresh_access_token_posts_to_the_stored_instance_url(monkeypatch):
    monkeypatch.setenv("SF_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("SF_OAUTH_CLIENT_SECRET", "sec")
    captured = {}

    def fake_post(url, data, timeout):
        captured["url"] = url
        captured["data"] = data
        return _FakeResp({"access_token": "fresh-token"})

    import requests
    monkeypatch.setattr(requests, "post", fake_post)

    tok = salesforce_oauth.refresh_access_token("rt-xyz", "https://acme.my.salesforce.com")
    assert tok == "fresh-token"
    assert captured["url"] == "https://acme.my.salesforce.com/services/oauth2/token"
    assert captured["data"]["grant_type"] == "refresh_token"
    assert captured["data"]["refresh_token"] == "rt-xyz"


def test_client_from_oauth_builds_a_client_with_the_fresh_token(monkeypatch):
    monkeypatch.setenv("SF_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("SF_OAUTH_CLIENT_SECRET", "sec")
    monkeypatch.setattr(salesforce_oauth, "refresh_access_token",
                        lambda rt, iu: "fresh-access-token")

    captured = {}

    class _FakeSession:
        def request(self, *a, **k):
            pass

    class _FakeSalesforce:
        def __init__(self, **kw):
            captured.update(kw)
            self.session = _FakeSession()

    import simple_salesforce
    monkeypatch.setattr(simple_salesforce, "Salesforce", _FakeSalesforce)

    client = salesforce_oauth.client_from_oauth("rt-xyz", "https://acme.my.salesforce.com")
    assert captured["instance_url"] == "acme.my.salesforce.com"
    assert captured["session_id"] == "fresh-access-token"
    assert isinstance(client, _FakeSalesforce)


# --------------------------------------------------------------------------
# _build_client's dispatch -- OAuth creds route to salesforce_oauth, not the
# JWT/password/SOAP paths, and are checked FIRST (no SF_USERNAME needed).
# --------------------------------------------------------------------------
def test_build_client_dispatches_oauth_creds_to_salesforce_oauth(monkeypatch):
    called = {}
    monkeypatch.setattr(salesforce_oauth, "client_from_oauth",
                        lambda rt, iu: called.update(rt=rt, iu=iu) or object())

    salesforce._build_client({
        "SF_OAUTH_REFRESH_TOKEN": "rt-1", "SF_OAUTH_INSTANCE_URL": "https://x.my.salesforce.com",
    })
    assert called == {"rt": "rt-1", "iu": "https://x.my.salesforce.com"}


def test_build_client_oauth_creds_never_need_sf_username(monkeypatch):
    """The dict has no SF_USERNAME at all -- must not KeyError."""
    monkeypatch.setattr(salesforce_oauth, "client_from_oauth", lambda rt, iu: "ok")
    result = salesforce._build_client({
        "SF_OAUTH_REFRESH_TOKEN": "rt", "SF_OAUTH_INSTANCE_URL": "https://x.my.salesforce.com",
    })
    assert result == "ok"


def test_redact_org_secret_treats_instance_url_as_safe_but_not_the_refresh_token():
    r = salesforce.redact_org_secret({
        "SF_OAUTH_REFRESH_TOKEN": "super-secret",
        "SF_OAUTH_INSTANCE_URL": "https://acme.my.salesforce.com",
    })
    assert r["SF_OAUTH_INSTANCE_URL"] == "https://acme.my.salesforce.com"
    assert "SF_OAUTH_REFRESH_TOKEN" not in r
    assert r["has_credentials"] is True
