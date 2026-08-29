"""
Persist an interpreter run to the `runs` table (migration 010).

Best-effort: a failed insert logs and returns None — recording a run must
never break the run itself. Set RUNS_DISABLED=1 to skip entirely (tests,
throwaway loops).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ingestion.scraper import get_supabase

log = logging.getLogger("interpreter.runs")


def _slim_retrieval(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {k: r.get(k) for k in ("doc_url", "heading_path", "rerank_score")}
        for r in (items or [])
    ]


def build_row(flow: dict, final: dict, *, case: dict, source: str,
              idempotency_key: str | None = None) -> dict[str, Any]:
    outcome = final.get("outcome") or {}
    case_id = case.get("case_id") or case.get("sf_id") or case.get("id")
    return {
        "flow_id": flow["flow_id"],
        "flow_version": flow.get("flow_version"),
        "tenant_id": flow["tenant_id"],
        "team": flow["team"],
        "source": source,
        "case_id": (str(case_id)[:200] or None) if case_id else None,
        "subject": (str(case.get("subject") or "")[:500] or None),
        "tier": final.get("tier"),
        "region": final.get("region"),
        "outcome": outcome.get("action"),
        "confidence": final.get("confidence"),
        "gate": final.get("confidence_gate"),
        "trace": final.get("trace") or [],
        "retrieval": _slim_retrieval(final.get("retrieval")),
        "sf_writeback": final.get("sf_writeback"),
        "case_payload": case,
        "idempotency_key": idempotency_key,
    }


def record_run(flow: dict, final: dict, *, case: dict, source: str = "api",
               idempotency_key: str | None = None, sb=None) -> str | None:
    if os.environ.get("RUNS_DISABLED"):
        return None
    try:
        sb = sb or get_supabase()
        row = build_row(flow, final, case=case, source=source, idempotency_key=idempotency_key)
        res = sb.table("runs").insert(row).execute()
        return res.data[0]["run_id"] if res.data else None
    except Exception as e:  # noqa: BLE001 -- never fail the run over telemetry
        log.warning("record_run failed: %s", e)
        return None
