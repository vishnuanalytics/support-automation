"""Route-level checks for the Case Monitor API, with no network."""

import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "http://localhost")
import api.main as m  # noqa: E402


@pytest.fixture
def client():
    return TestClient(m.app)


@pytest.mark.parametrize("method,path", [
    ("get", "/api/monitor/queue"), ("get", "/api/monitor/cases/1"),
    ("post", "/api/monitor/sync"), ("put", "/api/monitor/config"),
    ("post", "/api/monitor/cases/1/tag-team"), ("post", "/api/monitor/demo"),
    ("delete", "/api/monitor/slack"),
    ("get", "/api/monitor/escalation-marks"), ("put", "/api/monitor/escalation-marks/1"),
    ("delete", "/api/monitor/escalation-marks/1"),
    ("get", "/api/monitor/notification-views"), ("post", "/api/monitor/notification-views"),
])
def test_every_monitor_route_needs_a_token(client, method, path):
    assert client.request(method.upper(), path, json={}).status_code == 401
