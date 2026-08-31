"""deploy/run_all.py — supervisor restarts a dead child; health port responds."""

import importlib.util
import json
import pathlib
import sys
import threading
import time
import urllib.request

import pytest

_spec = importlib.util.spec_from_file_location(
    "run_all", pathlib.Path(__file__).resolve().parents[1] / "deploy" / "run_all.py"
)
run_all = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_all)


@pytest.fixture(autouse=True)
def _reset_stop():
    run_all._stop.clear()
    yield
    run_all._stop.set()


def test_health_endpoint_and_restart():
    # a child that exits almost immediately -> the supervisor must respawn it
    run_all.PROCS["dummy"] = [sys.executable, "-c", "import time; time.sleep(0.3)"]
    child = run_all.Child("dummy", run_all.PROCS["dummy"])

    run_all.start_health_server([child], 8791)
    t = threading.Thread(target=run_all.supervise, args=([child],), daemon=True)
    t.start()

    time.sleep(3.0)  # long enough for at least one exit + restart
    try:
        with urllib.request.urlopen("http://127.0.0.1:8791/") as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:      # 503 while a child is mid-backoff
        body = json.loads(e.read())

    assert "dummy" in body["procs"]
    assert set(body["procs"]["dummy"]) == {"alive", "pid", "restarts"}
    assert child.restarts >= 1, "child should have been restarted at least once"

    run_all._stop.set()
    t.join(timeout=5)
    assert not t.is_alive()
