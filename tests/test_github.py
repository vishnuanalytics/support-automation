"""2026-09-04 -- `interpreter/github.py` had zero dedicated test coverage
(confirmed by grep before writing this file) even though it's the module
`api/worker.py::_create_github_issue` calls to actually open a real GitHub
issue once a Slack approval lands. Offline; no network."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import github


class _FakeSb:
    def __init__(self, token: str | None):
        self._token = token

    def rpc(self, name, params):
        import json
        token = self._token

        class _Exec:
            def execute(self):
                if name != "integration_secret_get":
                    raise AssertionError(f"unexpected rpc {name!r}")
                return type("R", (), {"data": json.dumps({"token": token}) if token else None})()
        return _Exec()


# ── token_for ────────────────────────────────────────────────────────────
def test_token_for_prefers_the_tenant_vault_token():
    assert github.token_for("t1", _FakeSb("gh-tenant-token")) == "gh-tenant-token"


def test_token_for_falls_back_to_env_when_no_vault_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "gh-env-token")
    assert github.token_for("t1", _FakeSb(None)) == "gh-env-token"


def test_token_for_falls_back_to_env_with_no_tenant_id(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "gh-env-token")
    assert github.token_for(None, None) == "gh-env-token"


def test_token_for_raises_when_nothing_resolves(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="no GitHub token"):
        github.token_for("t1", _FakeSb(None))


# ── available ────────────────────────────────────────────────────────────
def test_available_true_when_a_token_resolves():
    assert github.available("t1", _FakeSb("tok")) is True


def test_available_false_when_nothing_resolves(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert github.available("t1", _FakeSb(None)) is False


# ── create_issue ─────────────────────────────────────────────────────────
def _fake_requests(monkeypatch, *, status=201, json_body=None, capture=None):
    class _Resp:
        status_code = status
        text = "boom"
        def json(self): return json_body or {"html_url": "https://github.com/a/b/issues/1", "number": 1}

    def _post(url, **kw):
        if capture is not None:
            capture.update(url=url, **kw)
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "post", _post)


def test_create_issue_posts_the_right_payload_and_headers(monkeypatch):
    cap = {}
    _fake_requests(monkeypatch, capture=cap)
    out = github.create_issue("tok123", "acme/ops", title="t", body="b",
                              labels=["bot"], assignees=["vishnu"])
    assert out == {"html_url": "https://github.com/a/b/issues/1", "number": 1}
    assert cap["url"] == "https://api.github.com/repos/acme/ops/issues"
    assert cap["json"] == {"title": "t", "body": "b", "labels": ["bot"], "assignees": ["vishnu"]}
    assert cap["headers"]["Authorization"] == "Bearer tok123"


def test_create_issue_omits_empty_labels_and_assignees(monkeypatch):
    cap = {}
    _fake_requests(monkeypatch, capture=cap)
    github.create_issue("tok", "acme/ops", title="t")
    assert "labels" not in cap["json"] and "assignees" not in cap["json"]


def test_create_issue_rejects_a_malformed_repo():
    with pytest.raises(ValueError, match="owner/name"):
        github.create_issue("tok", "not-a-repo", title="t")


def test_create_issue_raises_on_a_non_2xx_response(monkeypatch):
    _fake_requests(monkeypatch, status=403)
    with pytest.raises(RuntimeError, match="403"):
        github.create_issue("tok", "acme/ops", title="t")
