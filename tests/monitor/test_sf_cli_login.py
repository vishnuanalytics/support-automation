from types import SimpleNamespace

import pytest

from interpreter import salesforce, salesforce_oauth, vault_secrets
from interpreter.monitor import config, service


def test_parses_cli_auth_url():
    got = salesforce_oauth.parse_sfdx_auth_url("force://PlatformCLI::5Aep:abc@org.my.salesforce.com")
    assert got == {"client_id": "PlatformCLI", "client_secret": "", "refresh_token": "5Aep:abc",
                   "instance_url": "https://org.my.salesforce.com"}


def test_parses_cli_auth_url_with_no_client_part():
    got = salesforce_oauth.parse_sfdx_auth_url("force://:5Aep@org.my.salesforce.com")
    assert got["client_id"] == "PlatformCLI" and got["refresh_token"] == "5Aep"


@pytest.mark.parametrize("bad", ["", "https://x", "force://nope", "force://a::@x"])
def test_rejects_bad_auth_urls(bad):
    with pytest.raises(ValueError):
        salesforce_oauth.parse_sfdx_auth_url(bad)


def _capture_post(monkeypatch):
    sent = {}

    def post(url, data=None, timeout=None):
        sent.update(data)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"access_token": "t"})

    monkeypatch.setattr("requests.post", post)
    return sent


def test_public_client_refresh_sends_no_secret(monkeypatch):
    monkeypatch.delenv("SF_OAUTH_CLIENT_SECRET", raising=False)
    sent = _capture_post(monkeypatch)
    salesforce_oauth.refresh_access_token("r", "https://x", "PlatformCLI")
    assert sent["client_id"] == "PlatformCLI" and "client_secret" not in sent


def test_connected_app_refresh_still_uses_the_secret(monkeypatch):
    monkeypatch.setenv("SF_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("SF_OAUTH_CLIENT_SECRET", "sec")
    sent = _capture_post(monkeypatch)
    salesforce_oauth.refresh_access_token("r", "https://x")
    assert sent["client_secret"] == "sec"


def test_a_failed_login_never_falls_back_to_the_platform_org(monkeypatch):
    def env_client():
        raise AssertionError("must not use the platform org")

    def broken(creds):
        raise RuntimeError("invalid_grant secret-token")

    monkeypatch.setattr(salesforce, "_client", env_client)
    monkeypatch.setattr(salesforce, "_build_client", broken)
    monkeypatch.setattr(vault_secrets, "get", lambda *a, **k: {"SF_OAUTH_REFRESH_TOKEN": "r"})
    with pytest.raises(ValueError) as e:
        service._case_reader(None, "t", config.merge({}))
    assert "reconnect" in str(e.value) and "secret-token" not in str(e.value)


def test_no_stored_login_asks_you_to_connect(monkeypatch):
    monkeypatch.setattr(vault_secrets, "get", lambda *a, **k: {})
    with pytest.raises(ValueError, match="Connect the workspace"):
        service._case_reader(None, "t", config.merge({}))


def test_client_points_at_the_real_host(monkeypatch):
    monkeypatch.setattr(salesforce_oauth, "refresh_access_token", lambda *a: "t")
    client = salesforce_oauth.client_from_oauth("r", "https://acme.my.salesforce.com/", "PlatformCLI")
    assert client.sf_instance == "acme.my.salesforce.com"
