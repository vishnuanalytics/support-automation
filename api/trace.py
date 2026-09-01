"""
Phase 22 — one timeline per Case: every job, every run, every node, every
error, in order. Pure builders (`build_timeline`, `render_markdown`); the DB
fetch + the route live in api/main.py.

The "why" for a Case is already in `runs.trace` (per-node summary + data),
`runs.gate`, `runs.sf_writeback`, and `jobs.error`. This stitches them into
a single, time-ordered story so you don't SQL-spelunk three tables.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

_STALE_AFTER = timedelta(minutes=10)     # jobs.claim_job reclaim window (migration 041)
_TERMINAL_NODES = ("auto_reply", "ask_human", "handover", "notify", "clarify")


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _node_error(data: dict) -> str | None:
    for k in ("error", "err", "exception"):
        if data.get(k):
            return str(data[k])
    # sf_case / sf_writeback style: a status that reads like a failure
    st = str(data.get("status") or "")
    if st and ("no sf_id" in st or "fail" in st.lower() or "error" in st.lower()):
        return st
    return None


def build_timeline(
    *,
    key: str,
    runs: list[dict],
    jobs: list[dict],
    channel_errors: list[dict] | None = None,
    case_events: list[dict] | None = None,
) -> dict[str, Any]:
    """runs + jobs rows (already fetched, any order) -> a timeline + summary.
    `case_events` (Phase 27) is the Status / routing / breach spine."""
    runs = sorted(runs, key=lambda r: str(r.get("created_at") or ""))
    jobs = sorted(jobs, key=lambda j: str(j.get("created_at") or ""))
    now = datetime.now(timezone.utc)

    events: list[dict[str, Any]] = []
    errors: list[str] = []
    stale_jobs: list[str] = []
    failed_jobs: list[str] = []
    degraded = False
    total_ms = 0.0
    total_tokens = 0
    labels_written: dict[str, Any] = {}
    labels_skipped: dict[str, Any] = {}
    final_queue: str | None = None
    sf_id: str | None = None
    case_number: str | None = None

    # ---- jobs -----------------------------------------------------------
    for j in jobs:
        created = _ts(j.get("created_at"))
        status = j.get("status")
        err = j.get("error") or None
        if err:
            errors.append(f"job {j.get('kind')} ({j.get('job_id', '')[:8]}): {err[:6000]}")
        if status == "failed":
            failed_jobs.append(j.get("job_id"))
        if status == "running":
            locked = _ts(j.get("locked_at")) or _ts(j.get("updated_at"))
            if locked and now - locked > _STALE_AFTER:
                stale_jobs.append(j.get("job_id"))
        events.append({
            "ts": _iso(created),
            "kind": "job",
            "label": f"job · {j.get('kind')}",
            "status": status,
            "summary": (f"attempt {j.get('attempts')}/{j.get('max_attempts')}"
                        + (f" · retry {j.get('run_after')}" if j.get("run_after") and status != "done" else "")
                        + (" · STALE (worker died mid-job)" if j.get("job_id") in stale_jobs else "")),
            "error": err,
            "data": {"job_id": j.get("job_id"), "dedupe_key": j.get("dedupe_key"),
                     "run_after": j.get("run_after"), "locked_at": j.get("locked_at"),
                     "updated_at": j.get("updated_at")},
        })

    # ---- runs + their nodes ------------------------------------------------
    for run in runs:
        base = _ts(run.get("created_at")) or now
        cp = run.get("case_payload") or {}
        sf_id = sf_id or cp.get("sf_id") or run.get("case_id")
        case_number = case_number or cp.get("case_number")
        src = run.get("source") or "flow"
        fv = run.get("flow_version")
        events.append({
            "ts": _iso(base), "kind": "run_start",
            "label": f"run · {src}" + (f" · flow v{fv}" if fv else ""),
            "summary": f"idempotency_key={run.get('idempotency_key')}",
            "data": {"run_id": run.get("run_id"), "flow_id": run.get("flow_id"),
                     "team": run.get("team"), "tier": run.get("tier"),
                     "region": run.get("region")},
        })
        elapsed = 0.0
        for node in (run.get("trace") or []):
            data = node.get("data") or {}
            ms = float(data.get("elapsed_ms") or 0)
            elapsed += ms
            total_ms += ms
            tok = data.get("tokens") or {}
            if isinstance(tok, dict):
                total_tokens += int(tok.get("total") or 0)
            is_stub = bool(data.get("stub")) or "stub" in str(node.get("summary", "")).lower()
            if is_stub:
                degraded = True
            nerr = _node_error(data)
            if nerr:
                errors.append(f"{node.get('type')}: {nerr[:6000]}")
            if node.get("type") == "sf_writeback":
                labels_written = data.get("written") or labels_written
                labels_skipped = data.get("skipped") or labels_skipped
            if node.get("type") in _TERMINAL_NODES:
                asn = (data.get("assignment") or {})
                final_queue = (asn.get("queue") or data.get("label")
                               or data.get("target") or final_queue)
            events.append({
                "ts": _iso(base + timedelta(milliseconds=elapsed)),
                "kind": "node",
                "label": node.get("type"),
                "status": "stub" if is_stub else ("error" if nerr else "ok"),
                "summary": node.get("summary"),
                "error": nerr,
                "data": data,
            })
        events.append({
            "ts": _iso(base + timedelta(milliseconds=elapsed)),
            "kind": "run_end",
            "label": "outcome",
            "status": run.get("outcome"),
            "summary": (f"outcome={run.get('outcome')}"
                        f" · confidence={run.get('confidence')}"
                        + (f" · human={run.get('human_action')}" if run.get("human_action") else "")),
            "data": {"outcome": run.get("outcome"), "confidence": run.get("confidence"),
                     "gate": run.get("gate"), "human_action": run.get("human_action"),
                     "human_reply": run.get("human_reply")},
        })

    for ce in (channel_errors or []):
        if ce.get("last_error"):
            errors.append(f"channel {ce.get('kind')}: {ce['last_error']}")
            events.append({
                "ts": _iso(_ts(ce.get("last_poll_at"))), "kind": "channel",
                "label": f"channel · {ce.get('kind')}", "status": ce.get("status"),
                "summary": ce.get("last_error"), "error": ce.get("last_error"), "data": ce,
            })

    # ---- case_events (Phase 27 — the Status / routing / breach spine) ---
    for ev in sorted(case_events or [], key=lambda e: str(e.get("ts") or "")):
        transition = ""
        if ev.get("from_status") or ev.get("to_status"):
            transition = f" · {ev.get('from_status') or '∅'} → {ev.get('to_status') or '∅'}"
        bits = [b for b in (ev.get("routed_team") and f"team={ev['routed_team']}",
                            ev.get("reason")) if b]
        if str(ev.get("action")) == "breach":
            errors.append(f"SLA breach: Case {ev.get('case_number') or ev.get('case_sf_id')}")
        events.append({
            "ts": _iso(_ts(ev.get("ts"))),
            "kind": "case_event",
            "label": f"case · {ev.get('action')}",
            "status": ev.get("to_status"),
            "summary": (f"{ev.get('actor')}{transition}"
                        + (f" · {' · '.join(bits)}" if bits else "")),
            "data": ev,
        })

    events.sort(key=lambda e: (e["ts"] or "", e["kind"] != "job"))

    last_run = runs[-1] if runs else {}
    return {
        "key": key,
        "sf_id": sf_id,
        "case_number": case_number,
        "counts": {"runs": len(runs), "jobs": len(jobs), "events": len(events),
                   "case_events": len(case_events or []), "errors": len(errors)},
        "outcome": last_run.get("outcome"),
        "human_action": last_run.get("human_action"),
        "flow_version": last_run.get("flow_version"),
        "degraded_llm": degraded,
        "stale_jobs": stale_jobs,
        "failed_jobs": [j for j in failed_jobs if j],
        "errors": errors,
        "labels_written": labels_written,
        "labels_skipped": labels_skipped,
        "final_queue": final_queue,
        "total_ms": round(total_ms, 1),
        "total_tokens": total_tokens,
        "timeline": events,
    }


def render_markdown(t: dict[str, Any]) -> str:
    L = [f"# Trace — {t.get('case_number') or t.get('sf_id') or t['key']}", ""]
    L.append(f"- outcome: **{t.get('outcome')}**"
             + (f"  (human: {t['human_action']})" if t.get("human_action") else ""))
    L.append(f"- flow version: {t.get('flow_version')}   "
             f"runs: {t['counts']['runs']}   jobs: {t['counts']['jobs']}")
    L.append(f"- time: {t['total_ms']} ms   tokens: {t['total_tokens']}")
    if t.get("degraded_llm"):
        L.append("- ⚠️ **LLM ran in STUB mode** (Groq quota / rate-limit) — draft quality degraded")
    if t.get("stale_jobs"):
        L.append(f"- ⚠️ stale job(s): {', '.join(t['stale_jobs'])}")
    if t.get("failed_jobs"):
        L.append(f"- ❌ failed job(s): {', '.join(t['failed_jobs'])}")
    if t.get("final_queue"):
        L.append(f"- landed with: {t['final_queue']}")
    def _short(d: dict) -> dict:
        return {k: (v if not isinstance(v, str) or len(v) < 80 else v[:77] + "…")
                for k, v in (d or {}).items()}
    if t.get("labels_written"):
        L.append(f"- labels written: {_short(t['labels_written'])}")
    if t.get("labels_skipped"):
        L.append(f"- labels skipped: {_short(t['labels_skipped'])}")
    if t.get("errors"):
        L += ["", "## errors"] + [f"- {e}" for e in t["errors"]]
    L += ["", "## timeline"]
    for e in t["timeline"]:
        mark = {"job": "▸", "run_start": "┌", "run_end": "└", "node": "  •",
                "channel": "✉"}.get(e["kind"], "  ")
        tag = f" [{e['status']}]" if e.get("status") else ""
        L.append(f"{mark} {e['ts'] or '':<26} {e['label']}{tag}")
        if e.get("summary"):
            L.append(f"      {e['summary']}")
        if e.get("error"):
            L.append(f"      ERROR: {e['error']}")
    return "\n".join(L)
