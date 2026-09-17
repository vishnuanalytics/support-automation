"""
Run the REAL support pipeline for one case and pull out everything a
deepeval `LLMTestCase` needs: the customer question (input), the drafted
reply (actual_output) and the exact retrieved passages the draft was told
to ground itself in (retrieval_context / context).

Same entry point as `eval/e2e/run_e2e.py` — `build_graph(flow).invoke` —
so the numbers describe the same system that eval measures for
auto-send / escalation.
"""

from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("RUNS_DISABLED", "1")  # don't write eval runs to the runs table

from interpreter.builder import build_graph  # noqa: E402
from interpreter.loader import load_flow  # noqa: E402

ACME_SUPPORT = "11111111-1111-1111-1111-111111111111"


def get_flow(flow_id: str | None = None, tenant_id: str | None = None,
             team: str | None = None) -> dict:
    if tenant_id and team:
        return load_flow(tenant_id=tenant_id, team=team, status="published")
    return load_flow(flow_id=flow_id or ACME_SUPPORT)


def _contexts_for_draft(state: dict) -> list[str]:
    """The passages `h_draft` / `groundedness.check` actually fed the model:
    internal runbook hits, then prior-resolution text, then the confirmed
    (non-provisional) retrieved chunks. Mirrors interpreter/registry.py."""
    retrieval = state.get("retrieval") or []
    confirmed = [r for r in retrieval
                 if (r.get("entry_status") or "active") != "provisional"]
    internal = (state.get("internal_kb") or {}).get("matches") or []
    prior = state.get("prior_resolutions") or []

    out: list[str] = []
    for r in internal[:5]:
        t = (r.get("chunk_text") or "").strip()
        if t:
            out.append(f"[internal runbook] {t}")
    for p in prior[:3]:
        t = (p.get("resolution_text") or "").strip()
        if t:
            out.append(f"[prior case {p.get('case_number') or '?'}] {t}")
    for r in confirmed[:5]:
        t = (r.get("chunk_text") or "").strip()
        if t:
            src = r.get("doc_url") or r.get("title") or ""
            out.append(f"[{src}] {t}" if src else t)
    return out


def run_case(graph, case_row: dict) -> dict:
    """`case_row` = one line of the cases.jsonl (id/subject/body/account,
    optional gold_action). Returns a dict ready to build an LLMTestCase."""
    case = {
        "case_id": case_row["id"],
        "subject": case_row["subject"],
        "body": case_row["body"],
        "account": case_row.get("account") or {},
    }
    state = graph.invoke({"case": case, "trace": []})

    gate = state.get("confidence_gate") or {}
    draft = (state.get("draft") or "").strip()
    contexts = _contexts_for_draft(state)
    return {
        "id": case_row["id"],
        "input": f"Subject: {case_row['subject']}\n\n{case_row['body']}",
        "draft": draft,
        "contexts": contexts,
        "gold_action": case_row.get("gold_action"),
        "pred_action": (state.get("outcome") or {}).get("action"),
        "tier": state.get("tier"),
        "why": case_row.get("why"),
        "expected_output": case_row.get("expected_output"),
        "groundedness": round(float((state.get("groundedness") or {}).get("score", 0.0)), 4),
        "groundedness_backend": (state.get("groundedness") or {}).get("backend"),
        "retrieval_score": round(float(gate.get("retrieval_score", 0.0)), 4),
        "draft_confidence": round(float(gate.get("draft_confidence", 0.0)), 4),
    }
