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


def _token_usage(trace: list[dict[str, Any]]) -> tuple[int, dict[str, int]]:
    """P9 — roll each LLM-calling node's `data.tokens.total` (classify /
    draft / ai_prompt; see interpreter/registry.py) into a run-level total
    and a per-model breakdown, for the usage & billing dashboard."""
    total = 0
    by_model: dict[str, int] = {}
    for t in trace or []:
        tok = (t.get("data") or {}).get("tokens")
        if not tok or not tok.get("total"):
            continue
        n = int(tok["total"])
        total += n
        model = (t.get("data") or {}).get("model") or "unknown"
        by_model[model] = by_model.get(model, 0) + n
    return total, by_model


def build_row(flow: dict, final: dict, *, case: dict, source: str,
              idempotency_key: str | None = None) -> dict[str, Any]:
    outcome = final.get("outcome") or {}
    # P5d — a generic (trigger/webhook) run has no Case; record its `context`
    # payload so `case_payload` / the trace view still reconstruct the run.
    if not case:
        ctx = final.get("context") or {}
        case = {k: v for k, v in ctx.items() if not str(k).startswith("_")}
    case_id = case.get("case_id") or case.get("sf_id") or case.get("id")
    action = outcome.get("action")
    # a run that went to a human, on a real Case, gets a resolution check
    # (Phase 20m: an agent CaseComment -> the bot polishes it into a customer
    # reply and sends it). `notify` and `clarify`/`need_info` count too — a rep
    # answering in Chatter/comments should still reach the customer.
    pending = (action in ("ask_human", "handover", "notify", "need_info")
               and bool(case.get("sf_id") or case.get("id")))
    tokens_total, tokens_by_model = _token_usage(final.get("trace") or [])
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
        "outcome": action,
        "draft": final.get("draft"),
        "clarify_round": final.get("clarify_round"),
        "human_action": "pending" if pending else None,
        "confidence": final.get("confidence"),
        "gate": final.get("confidence_gate"),
        "trace": final.get("trace") or [],
        "tokens_total": tokens_total,
        "tokens_by_model": tokens_by_model,
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
        run_id = res.data[0]["run_id"] if res.data else None
    except Exception as e:  # noqa: BLE001 -- never fail the run over telemetry
        log.warning("record_run failed: %s", e)
        return None

    # Phase 28 step 3 — best-effort: warn (never block) once a tenant crosses
    # 80%/100% of its plan's monthly quota.
    try:
        from interpreter import billing

        billing.check_and_warn(sb, row["tenant_id"])
    except Exception as e:  # noqa: BLE001
        log.warning("billing.check_and_warn failed: %s", e)

    # Phase 16: a task_dispatch node raised an action_requests row with no
    # run_id (the run didn't exist yet) — link it now. `action_request_id`
    # is a top-level state key so it survives a later terminal node.
    ar_id = final.get("action_request_id") or (final.get("outcome") or {}).get("action_request_id")
    if run_id and ar_id:
        try:
            sb.table("action_requests").update({"run_id": run_id}).eq("id", ar_id).execute()
        except Exception as e:  # noqa: BLE001
            log.warning("could not link action_request %s to run %s: %s", ar_id, run_id, e)

    # Phase 27c — the flow's nodes wrote `case_events` rows with no run_id
    # (the run didn't exist yet). Stamp them now, best-effort.
    sf_id = case.get("sf_id") or case.get("id")
    if run_id and sf_id:
        try:
            import datetime as _dt

            from interpreter import case_events

            since = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=15)).isoformat()
            case_events.link_run(sb, case_sf_id=str(sf_id), run_id=run_id, since_iso=since)
        except Exception as e:  # noqa: BLE001
            log.warning("could not link case_events to run %s: %s", run_id, e)

    # close the loop later: if this went to a human on a real Case, schedule a
    # resolution check (Phase 11). Best-effort. Phase 24: skipped when a Slack
    # reasoning session owns the case — that dialogue is the resolution path.
    if run_id and row.get("human_action") == "pending":
        try:
            import datetime as _dt

            from interpreter import jobs

            # reasoning_sessions.case_id is the SF record id (500…); row.case_id
            # is the CaseNumber, so match on the id from the payload.
            sf_id = case.get("sf_id") or case.get("id")
            has_session = False
            if sf_id:
                try:
                    has_session = bool(
                        sb.table("reasoning_sessions").select("session_id")
                        .eq("case_id", sf_id).not_.in_("state", ("sent", "abandoned"))
                        .limit(1).execute().data)
                except Exception:  # noqa: BLE001
                    has_session = False
            if has_session:
                log.info("run %s: reasoning session owns the case — no check_resolution", run_id)
            else:
                delay = _int_env("FEEDBACK_DELAY_MIN", 20)
                run_after = (_dt.datetime.now(_dt.timezone.utc)
                             + _dt.timedelta(minutes=delay)).isoformat()
                jobs.enqueue("check_resolution", {"run_id": run_id},
                             dedupe_key=run_id, run_after=run_after, sb=sb)
        except Exception as e:  # noqa: BLE001
            log.warning("could not schedule resolution check for %s: %s", run_id, e)

    return run_id


def _int_env(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        log.warning("%s=%r is not an int; using %d", name, os.environ.get(name), default)
        return default
