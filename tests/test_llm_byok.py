"""
Chunk 3 of the 2026-09-04 onboarding/robustness work: self-serve LLM
provider keys (BYOK) + the model roster endpoint the Inspector's picker
reads. Live-verified against the real Globex tenant (same auth_headers
fixture as tests/test_api.py) rather than mocked — this exercises the
real `tenant_integrations` upsert/merge, not just the Python-level
`interpreter.llm._tenant_keys` resolution already covered by manual
smoke-testing during development.

    pytest tests/test_llm_byok.py -m integration
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402

client = TestClient(app)

GLOBEX_TENANT = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(scope="module")
def auth_headers():
    if os.environ.get("SUPABASE_ANON_KEY", "test-anon-key") == "test-anon-key":
        pytest.skip("no real SUPABASE_ANON_KEY — integration tests skipped")
    from supabase import create_client

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_ANON_KEY"])
    sess = sb.auth.sign_in_with_password(
        {"email": "globex-owner@example.test", "password": "editor-test-pw-8891"}
    )
    return {"Authorization": f"Bearer {sess.session.access_token}"}


@pytest.mark.integration
def test_models_endpoint_lists_the_real_roster(auth_headers):
    r = client.get(f"/api/models?tenant_id={GLOBEX_TENANT}", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    ids = {m["id"] for m in body["models"]}
    assert "claude-sonnet-5" in ids and "openai/gpt-oss-120b" in ids
    by_id = {m["id"]: m for m in body["models"]}
    assert by_id["claude-sonnet-5"]["provider"] == "anthropic"
    assert body["default_model"] and body["fast_model"]


@pytest.mark.integration
def test_llm_key_put_get_delete_round_trips_for_real(auth_headers):
    """Save a (fake, harmless) OpenRouter key, confirm the status endpoint
    reflects it and never echoes the key itself, then remove it and confirm
    it's gone — a real round trip through tenant_integrations, not a mock."""
    before = client.get(f"/api/integrations/llm?tenant_id={GLOBEX_TENANT}", headers=auth_headers).json()

    r = client.put("/api/integrations/llm", headers=auth_headers,
                   json={"provider": "openrouter", "api_key": "sk-or-test-fixture-only",
                         "tenant_id": GLOBEX_TENANT})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant"]["openrouter"] is True
    assert "api_key" not in body and "sk-or-test-fixture-only" not in str(body)

    got = client.get(f"/api/integrations/llm?tenant_id={GLOBEX_TENANT}", headers=auth_headers).json()
    assert got["tenant"]["openrouter"] is True
    # saving one provider must not disturb another tenant-key field already there
    assert got["tenant"]["groq"] == before["tenant"]["groq"]

    # the resolver (llm.py) must actually see this tenant's key now, cache
    # invalidation included — this is the exact mechanism a real complete()
    # call relies on.
    from interpreter import llm as llmmod
    assert llmmod._tenant_keys(GLOBEX_TENANT).get("openrouter") == "sk-or-test-fixture-only"

    d = client.delete(f"/api/integrations/llm/openrouter?tenant_id={GLOBEX_TENANT}", headers=auth_headers)
    assert d.status_code == 204

    after = client.get(f"/api/integrations/llm?tenant_id={GLOBEX_TENANT}", headers=auth_headers).json()
    assert after["tenant"]["openrouter"] is False
    assert llmmod._tenant_keys(GLOBEX_TENANT).get("openrouter") is None


@pytest.mark.integration
def test_llm_key_rejects_unknown_provider(auth_headers):
    r = client.put("/api/integrations/llm", headers=auth_headers,
                   json={"provider": "not-a-real-provider", "api_key": "x",
                         "tenant_id": GLOBEX_TENANT})
    assert r.status_code == 422
