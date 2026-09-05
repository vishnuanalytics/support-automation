"""Multi-provider connectors, step 3 -- offline tests for the Freshchat
channel's config model + pure webhook helpers (signature verification,
message parsing). No network, no Supabase."""

from __future__ import annotations

import base64

import pytest

from interpreter.freshchat import (
    FreshchatConfig,
    available,
    parse_webhook_message,
    save_channel,
    verify_signature,
)


# ── config model ─────────────────────────────────────────────────────────
def test_from_row_defaults():
    cfg = FreshchatConfig.from_row(
        "t", {"domain": "acme.freshchat.com"}, "active",
        {"api_token": "tok", "webhook_public_key": "pem"},
    )
    assert cfg.domain == "acme.freshchat.com" and cfg.team == "support"
    assert cfg.status == "active" and cfg.api_token == "tok"


def test_base_url_strips_scheme_and_trailing_slash():
    cfg = FreshchatConfig(tenant_id="t", domain="https://acme.freshchat.com/")
    assert cfg.base_url == "https://acme.freshchat.com/v2"


def test_base_url_empty_when_no_domain():
    assert FreshchatConfig(tenant_id="t").base_url == ""


def test_public_status_never_includes_the_secret():
    cfg = FreshchatConfig.from_row(
        "t", {"domain": "acme.freshchat.com"}, "active",
        {"api_token": "hunter2", "webhook_public_key": "-----BEGIN PUBLIC KEY-----"},
    )
    status = cfg.public_status()
    assert "hunter2" not in str(status)
    assert "-----BEGIN" not in str(status)
    assert status["configured"] is True and status["signature_verification"] is True


def test_repr_never_includes_the_secret():
    cfg = FreshchatConfig.from_row("t", {}, "active", {"api_token": "hunter2"})
    assert "hunter2" not in repr(cfg)


def test_available_requires_token_and_domain():
    assert not available(None)
    assert not available(FreshchatConfig(tenant_id="t"))
    assert not available(FreshchatConfig(tenant_id="t", api_token="tok"))  # no domain
    assert available(FreshchatConfig(tenant_id="t", domain="acme.freshchat.com", api_token="tok"))


# ── storage ──────────────────────────────────────────────────────────────
class _FakeSB:
    def __init__(self):
        self.upserts: list[dict] = []
        self.deletes: list[tuple] = []
        self._vault: dict = {}
        self._pending = None    # ("rpc", name, params) | ("delete", table)

    def table(self, name):
        self._table = name
        return self

    def select(self, *_a):
        return self

    def eq(self, *_a):
        return self

    def rpc(self, name, params):
        self._pending = ("rpc", name, params)
        return self

    def upsert(self, row, on_conflict=None):
        assert on_conflict == "tenant_id,kind,org_label"
        self.upserts.append(row)
        self._pending = ("upsert",)
        return self

    def delete(self):
        self.deletes.append((self._table,))
        self._pending = ("delete",)
        return self

    def execute(self):
        kind, *rest = self._pending
        if kind == "rpc":
            name, params = rest
            if name == "integration_secret_put":
                self._vault[params["p_kind"]] = params["p_plaintext"]
                return type("R", (), {"data": "vault-id"})()
            if name == "integration_secret_get":
                return type("R", (), {"data": self._vault.get(params["p_kind"])})()
            if name == "integration_secret_delete":
                self._vault.pop(params["p_kind"], None)
                return type("R", (), {"data": None})()
            raise AssertionError(f"unexpected rpc {name}")
        if kind == "upsert":
            row = self.upserts[-1]
            return type("R", (), {"data": [{"config": row["config"], "status": row["status"]}]})()
        return type("R", (), {"data": []})()


def test_save_channel_merges_secret_and_upserts_the_right_conflict_target():
    sb = _FakeSB()
    cfg = FreshchatConfig(tenant_id="t", domain="acme.freshchat.com", team="csm")
    save_channel(cfg, sb, api_token="tok1")
    save_channel(cfg, sb, webhook_public_key="pem1")   # a later save updates just the key
    import json
    stored = json.loads(sb._vault["freshchat"])
    assert stored == {"api_token": "tok1", "webhook_public_key": "pem1"}
    assert sb.upserts[-1]["config"] == {"domain": "acme.freshchat.com", "team": "csm",
                                        "auto_send_enabled": False}


def test_delete_channel_clears_vault_and_row():
    sb = _FakeSB()
    sb._vault["freshchat"] = '{"api_token": "tok"}'
    from interpreter.freshchat import delete_channel
    delete_channel("t", sb)
    assert "freshchat" not in sb._vault
    assert sb.deletes == [("tenant_integrations",)]


# ── signature verification (pure, real RSA) ──────────────────────────────
@pytest.fixture(scope="module")
def keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv, pub_pem


def _sign(priv, body: bytes) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    sig = priv.sign(body, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


def test_verify_signature_accepts_a_real_valid_signature(keypair):
    priv, pub_pem = keypair
    body = b'{"event":"message_create"}'
    assert verify_signature(pub_pem, body, _sign(priv, body)) is True


def test_verify_signature_rejects_a_tampered_body(keypair):
    priv, pub_pem = keypair
    body = b'{"event":"message_create"}'
    sig = _sign(priv, body)
    assert verify_signature(pub_pem, body + b"tampered", sig) is False


def test_verify_signature_rejects_wrong_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    priv_a = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_b = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_b_pem = priv_b.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    body = b"hello"
    sig = _sign(priv_a, body)
    assert verify_signature(pub_b_pem, body, sig) is False


def test_verify_signature_fails_closed_on_missing_inputs():
    assert verify_signature("", b"body", "sig") is False
    assert verify_signature("pem", b"body", None) is False
    assert verify_signature("not-a-real-pem", b"body", "c2ln") is False


# ── webhook message parsing (pure) ────────────────────────────────────────
def test_parse_webhook_message_extracts_a_customer_message():
    body = {
        "actor": {"actor_type": "user", "actor_id": "u1"},
        "data": {"message": {"conversation_id": "c1",
                             "message_parts": [{"text": {"content": "Hi, I need help"}}]}},
    }
    out = parse_webhook_message(body)
    assert out == {"conversation_id": "c1", "text": "Hi, I need help", "actor_id": "u1",
                  "message_id": None}


def test_parse_webhook_message_joins_multiple_parts():
    body = {
        "actor": {"actor_type": "user"},
        "data": {"message": {"conversation_id": "c1", "message_parts": [
            {"text": {"content": "part one"}}, {"text": {"content": "part two"}},
        ]}},
    }
    assert parse_webhook_message(body)["text"] == "part one part two"


def test_parse_webhook_message_ignores_agent_and_bot_echoes():
    for actor_type in ("agent", "system"):
        body = {"actor": {"actor_type": actor_type},
               "data": {"message": {"conversation_id": "c1",
                                    "message_parts": [{"text": {"content": "hi"}}]}}}
        assert parse_webhook_message(body) is None


def test_parse_webhook_message_ignores_empty_text():
    body = {"actor": {"actor_type": "user"},
           "data": {"message": {"conversation_id": "c1", "message_parts": []}}}
    assert parse_webhook_message(body) is None


def test_parse_webhook_message_ignores_missing_conversation_id():
    body = {"actor": {"actor_type": "user"},
           "data": {"message": {"message_parts": [{"text": {"content": "hi"}}]}}}
    assert parse_webhook_message(body) is None


def test_parse_webhook_message_tolerates_a_flatter_shape():
    """Defensive against the vendor's own documented shape variance --
    conversation_id/actor_type sitting directly on `data`/the message dict
    rather than nested under a separate `actor` object."""
    body = {"data": {"conversation_id": "c1", "actor_type": "user",
                     "message_parts": [{"text": {"content": "hi"}}]}}
    assert parse_webhook_message(body) == {"conversation_id": "c1", "text": "hi", "actor_id": None,
                                           "message_id": None}


def test_parse_webhook_message_extracts_the_message_id_when_present():
    body = {
        "actor": {"actor_type": "user"},
        "data": {"message": {"id": "m1", "conversation_id": "c1",
                             "message_parts": [{"text": {"content": "hi"}}]}},
    }
    assert parse_webhook_message(body)["message_id"] == "m1"


def test_parse_webhook_message_handles_empty_body():
    assert parse_webhook_message({}) is None
    assert parse_webhook_message(None) is None


# ── outbound send (dry-run without creds) ─────────────────────────────────
def test_send_message_dry_runs_without_credentials():
    from interpreter.freshchat import send_message

    out = send_message(FreshchatConfig(tenant_id="t"), "c1", "hello")
    assert out == {"sent": False, "dry_run": True, "reason": "freshchat not connected"}
