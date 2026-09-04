"""2026-09-04 -- `interpreter/vault_secrets.py`, the thin wrapper over the
Vault-backed `integration_secret_put/get/delete` RPCs (migration 035) that
every secret-holding integration (salesforce/slack/google/llm) now uses
instead of reading/writing `tenant_integrations.secret` in the clear."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import vault_secrets


class _FakeSb:
    def __init__(self):
        self.vault: dict[tuple[str, str], str] = {}

    def rpc(self, name, params):
        vault = self.vault

        class _Exec:
            def execute(self):
                if name == "integration_secret_get":
                    data = vault.get((params["p_tenant"], params["p_kind"]))
                elif name == "integration_secret_put":
                    vault[(params["p_tenant"], params["p_kind"])] = params["p_plaintext"]
                    data = "00000000-0000-0000-0000-000000000000"
                elif name == "integration_secret_delete":
                    vault.pop((params["p_tenant"], params["p_kind"]), None)
                    data = None
                else:
                    raise AssertionError(f"unexpected rpc {name!r}")
                return type("R", (), {"data": data})()
        return _Exec()


class _BoomSb:
    def rpc(self, name, params):
        raise RuntimeError("network is down")


def test_get_with_nothing_stored_is_an_empty_dict():
    assert vault_secrets.get("t1", "slack", sb=_FakeSb()) == {}


def test_get_with_no_tenant_id_is_an_empty_dict():
    assert vault_secrets.get(None, "slack", sb=_FakeSb()) == {}


def test_put_then_get_round_trips():
    sb = _FakeSb()
    vault_id = vault_secrets.put("t1", "slack", {"bot_token": "xoxb-1"}, sb=sb)
    assert vault_id
    assert vault_secrets.get("t1", "slack", sb=sb) == {"bot_token": "xoxb-1"}


def test_put_overwrites_the_prior_value():
    sb = _FakeSb()
    vault_secrets.put("t1", "slack", {"bot_token": "old"}, sb=sb)
    vault_secrets.put("t1", "slack", {"bot_token": "new"}, sb=sb)
    assert vault_secrets.get("t1", "slack", sb=sb) == {"bot_token": "new"}


def test_different_kinds_and_tenants_never_collide():
    sb = _FakeSb()
    vault_secrets.put("t1", "salesforce:prod", {"SF_USERNAME": "a"}, sb=sb)
    vault_secrets.put("t1", "salesforce:sandbox", {"SF_USERNAME": "b"}, sb=sb)
    vault_secrets.put("t2", "salesforce:prod", {"SF_USERNAME": "c"}, sb=sb)
    assert vault_secrets.get("t1", "salesforce:prod", sb=sb) == {"SF_USERNAME": "a"}
    assert vault_secrets.get("t1", "salesforce:sandbox", sb=sb) == {"SF_USERNAME": "b"}
    assert vault_secrets.get("t2", "salesforce:prod", sb=sb) == {"SF_USERNAME": "c"}


def test_delete_removes_it():
    sb = _FakeSb()
    vault_secrets.put("t1", "google", {"refresh_token": "r"}, sb=sb)
    vault_secrets.delete("t1", "google", sb=sb)
    assert vault_secrets.get("t1", "google", sb=sb) == {}


def test_get_degrades_to_empty_on_a_broken_client():
    assert vault_secrets.get("t1", "slack", sb=_BoomSb()) == {}


def test_put_degrades_to_none_on_a_broken_client():
    assert vault_secrets.put("t1", "slack", {"x": "y"}, sb=_BoomSb()) is None


def test_delete_never_raises_on_a_broken_client():
    vault_secrets.delete("t1", "slack", sb=_BoomSb())  # just must not raise
