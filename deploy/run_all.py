"""
All-in-one supervisor for single-container hosts (Hugging Face Spaces,
Render, Koyeb, a lone VM): runs `worker` + `cdc` + `poller` as child
processes, restarts any that exit (exponential backoff, reset after a
minute healthy), and serves a health JSON on $PORT (default 7860) so the
platform sees a live port and a keep-alive pinger has a target.

    python deploy/run_all.py
    python deploy/run_all.py --only worker,cdc      # a subset
    PORT=8080 python deploy/run_all.py

For multi-service platforms (Railway) use the root Dockerfile + Procfile
instead — one process per service is cleaner there.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROCS: dict[str, list[str]] = {
    "worker": [sys.executable, "-m", "api.worker"],
    "cdc": [sys.executable, "-m", "ingestion.sf_cdc_watch"],
    "poller": [sys.executable, "-m", "ingestion.email_watch", "--interval", "15"],
}

_stop = threading.Event()
_POLL = 1.0          # seconds between liveness checks


class Child:
    def __init__(self, name: str, cmd: list[str]) -> None:
        self.name, self.cmd = name, cmd
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0
        self.restarts = -1          # first start() -> 0
        self.backoff = 1.0

    def start(self) -> None:
        self.proc = subprocess.Popen(self.cmd)
        self.started_at = time.time()
        self.restarts += 1

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


def supervise(children: list[Child]) -> None:
    for c in children:
        c.start()
        print(f"[run_all] started {c.name} pid={c.proc.pid}", flush=True)  # type: ignore[union-attr]

    while not _stop.is_set():
        for c in children:
            if c.alive():
                if c.backoff > 1.0 and time.time() - c.started_at > 60:
                    c.backoff = 1.0
                continue
            rc = c.proc.returncode if c.proc else "?"
            print(f"[run_all] {c.name} exited rc={rc}; restart in {c.backoff:.0f}s", flush=True)
            if _stop.wait(c.backoff):
                break
            c.start()
            c.backoff = min(c.backoff * 2, 60.0)
            print(f"[run_all] restarted {c.name} pid={c.proc.pid} (#{c.restarts})", flush=True)  # type: ignore[union-attr]
        _stop.wait(_POLL)

    for c in children:
        if c.alive():
            c.proc.terminate()  # type: ignore[union-attr]
    for c in children:
        try:
            if c.proc:
                c.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            c.proc.kill()  # type: ignore[union-attr]


def start_health_server(children: list[Child], port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            ok = all(c.alive() for c in children)
            body = json.dumps({
                "ok": ok,
                "procs": {
                    c.name: {"alive": c.alive(),
                             "pid": c.proc.pid if c.proc else None,
                             "restarts": c.restarts}
                    for c in children
                },
            }).encode()
            self.send_response(200 if ok else 503)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):  # silence
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[run_all] health on :{port}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="deploy.run_all")
    ap.add_argument("--only", help="comma list of: " + ",".join(PROCS))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "7860")))
    args = ap.parse_args(argv)

    names = [n.strip() for n in (args.only.split(",") if args.only else list(PROCS)) if n.strip()]
    bad = [n for n in names if n not in PROCS]
    if bad:
        ap.error(f"unknown proc(s): {bad}")
    children = [Child(n, PROCS[n]) for n in names]

    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    signal.signal(signal.SIGINT, lambda *_: _stop.set())

    start_health_server(children, args.port)
    supervise(children)
    print("[run_all] stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
