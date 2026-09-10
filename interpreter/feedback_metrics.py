"""
Response Quality Feedback Loop chunk F (2026-09-10) — the dashboard that
ties chunks B-E's numbers together. Each chunk's data has existed as a raw
API field or its own endpoint since it shipped (`runs.correction_analysis`,
`reasoning_sessions.session_analysis`, `draft`'s `exemplars_used` trace
field, `action_requests(kind='gate_tuning')`) but nothing summarized them
in one place for a human to actually look at trends.

`compute(sb, tenant_id, days=30)` reads all four and returns the numbers
`ReviewView.tsx`'s new "Response quality" section renders. Same shape and
degrade-to-zero discipline as `interpreter/kil_metrics.py::compute()` —
read-only, every table read degrades to empty/zero rather than raising.
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

log = logging.getLogger("interpreter.feedback_metrics")

_GATE_STATUSES = ("pending", "approved", "rejected")
_EXEMPLAR_SAMPLE = 500


def _avg_severity(rows: list[dict], field: str) -> float | None:
    vals = [r[field].get("severity") for r in rows
            if isinstance(r.get(field), dict) and r[field].get("severity") is not None]
    return round(statistics.mean(vals), 3) if vals else None


def _exemplars_used(trace: list | None) -> int:
    for step in (trace or []):
        if step.get("type") == "draft":
            return int((step.get("data") or {}).get("exemplars_used") or 0)
    return 0


def compute(sb, tenant_id: str, *, days: int = 30) -> dict:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    tid = str(tenant_id)

    # ── corrections (chunk B) ────────────────────────────────────────
    try:
        corr_rows = (sb.table("runs")
                    .select("run_id, subject, correction_analysis, created_at")
                    .eq("tenant_id", tid).not_.is_("correction_analysis", "null")
                    .gte("created_at", since).order("created_at", desc=True)
                    .limit(500).execute().data or [])
    except Exception as e:  # noqa: BLE001
        log.warning("feedback_metrics corrections: %s", e)
        corr_rows = []
    corrections = {
        "total_judged": len(corr_rows),
        "by_category": dict(Counter(
            (r.get("correction_analysis") or {}).get("category") or "unknown" for r in corr_rows)),
        "avg_severity": _avg_severity(corr_rows, "correction_analysis"),
        "recent": [{
            "run_id": r["run_id"], "subject": r.get("subject"),
            "category": (r.get("correction_analysis") or {}).get("category"),
            "severity": (r.get("correction_analysis") or {}).get("severity"),
            "summary": (r.get("correction_analysis") or {}).get("summary"),
            "created_at": r.get("created_at"),
        } for r in corr_rows[:5]],
    }

    # ── reasoning sessions (chunk C) ──────────────────────────────────
    try:
        sess_rows = (sb.table("reasoning_sessions")
                    .select("session_id, case_number, session_analysis, updated_at")
                    .eq("tenant_id", tid).not_.is_("session_analysis", "null")
                    .gte("updated_at", since).order("updated_at", desc=True)
                    .limit(500).execute().data or [])
    except Exception as e:  # noqa: BLE001
        log.warning("feedback_metrics sessions: %s", e)
        sess_rows = []
    sessions = {
        "total_judged": len(sess_rows),
        "by_category": dict(Counter(
            (r.get("session_analysis") or {}).get("category") or "unknown" for r in sess_rows)),
        "avg_severity": _avg_severity(sess_rows, "session_analysis"),
        "recent": [{
            "session_id": r["session_id"], "case_number": r.get("case_number"),
            "category": (r.get("session_analysis") or {}).get("category"),
            "severity": (r.get("session_analysis") or {}).get("severity"),
            "summary": (r.get("session_analysis") or {}).get("summary"),
            "updated_at": r.get("updated_at"),
        } for r in sess_rows[:5]],
    }

    # ── exemplar usage (chunk D) — sampled, not exhaustive ────────────
    try:
        draft_rows = (sb.table("runs").select("trace")
                      .eq("tenant_id", tid).not_.is_("draft", "null")
                      .gte("created_at", since).order("created_at", desc=True)
                      .limit(_EXEMPLAR_SAMPLE).execute().data or [])
    except Exception as e:  # noqa: BLE001
        log.warning("feedback_metrics exemplars: %s", e)
        draft_rows = []
    used = sum(1 for r in draft_rows if _exemplars_used(r.get("trace")) > 0)
    exemplars = {
        "draft_runs_sampled": len(draft_rows),
        "draft_runs_with_exemplars": used,
        "usage_rate": round(used / len(draft_rows), 3) if draft_rows else None,
    }

    # ── gate-tuning proposals (chunk E) ───────────────────────────────
    try:
        gt_rows = (sb.table("action_requests")
                  .select("id, status, payload, created_at, decided_at")
                  .eq("tenant_id", tid).eq("kind", "gate_tuning")
                  .order("created_at", desc=True).limit(200).execute().data or [])
    except Exception as e:  # noqa: BLE001
        log.warning("feedback_metrics gate_tuning: %s", e)
        gt_rows = []
    by_status = Counter(r.get("status") or "?" for r in gt_rows)
    gate_tuning = {
        **{s: by_status.get(s, 0) for s in _GATE_STATUSES},
        "recent": [{
            "id": r["id"], "status": r.get("status"),
            "title": (r.get("payload") or {}).get("title"),
            "tier": (r.get("payload") or {}).get("tier"),
            "current_threshold": (r.get("payload") or {}).get("current_threshold"),
            "suggested_threshold": (r.get("payload") or {}).get("suggested_threshold"),
            "created_at": r.get("created_at"),
        } for r in gt_rows[:5]],
    }

    return {
        "window_days": days,
        "corrections": corrections,
        "sessions": sessions,
        "exemplars": exemplars,
        "gate_tuning": gate_tuning,
    }
