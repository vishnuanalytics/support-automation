"""
Phase 29 step 3 — does the `agent` node's multi-hop reformulation loop
actually beat a single retrieve pass on hard, keyword-heavy questions?

Step 2 built the `agent` node (retrieve+draft, reformulate the query and
retry when the draft's own groundedness score is low, up to
`max_iterations`) and it's already live on Acme/support's real published
flow. `ingestion/eval/run_eval.py --qrels hard` can't measure it directly
-- that harness scores raw retrieval in isolation and never touches
draft/groundedness, so it can't exercise the loop's actual decision (which
depends on a draft's groundedness score, not just retrieval). This script
is the purpose-built comparison PROJECT_SCOPE.md flagged as the one thing
left in step 3.

For each of the 10 hand-labelled hard questions (`ingestion/eval/
qrels_hard.jsonl` -- multi-hop / keyword-heavy, the exact weakness `agent`
targets) we run:

  baseline   one direct h_retrieve() call -- zero LLM cost.
  agent      one h_agent() call using Acme's REAL live agent-node config
             (pulled from the published flow, not guessed) -- up to
             max_iterations retrieve+draft+reformulate rounds.

Both sides are scored through the *same* hit@{1,3,5,10}/MRR@10 function
run_eval.py already uses, so the numbers are directly comparable to the
dense/sparse/hybrid/hybrid_rerank baselines it prints.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (+ GROQ_API_KEY for real drafts --
without it every draft is the deterministic offline stub and the agent
loop never reformulates, which is still a valid, if less interesting,
"lower bound" run).

    python -m eval.agent.run_agent_eval
    python -m eval.agent.run_agent_eval --flow <flow_id>
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from dotenv import load_dotenv

load_dotenv()
import os  # noqa: E402

os.environ["RUNS_DISABLED"] = "1"

from ingestion.eval.run_eval import load_qrels, score  # noqa: E402
from interpreter.loader import load_flow  # noqa: E402
from interpreter.registry import h_agent, h_retrieve  # noqa: E402

ACME_SUPPORT = "11111111-1111-1111-1111-111111111111"


def _agent_config(flow: dict) -> dict:
    for n in flow["nodes"]:
        if n["type"] == "agent":
            return n["config"]
    sys.exit(f"flow {flow['flow_id']!r} has no `agent` node -- pass --flow with one")


def _case(q: dict, tenant_id: str) -> dict:
    return {"case": {"case_id": q["id"], "subject": q["question"], "body": "",
                     "account": {"customer_type": "basic"}},
           "tenant_id": tenant_id, "trace": []}


def run_baseline(q: dict, tenant_id: str, retrieve_cfg: dict) -> list[str]:
    out = h_retrieve(_case(q, tenant_id), {**retrieve_cfg, "_node_id": "eval"})
    return [r["doc_url"] for r in (out.get("retrieval") or []) if r.get("doc_url")]


def run_agent(q: dict, tenant_id: str, agent_cfg: dict) -> tuple[list[str], int, int]:
    out = h_agent(_case(q, tenant_id), {**agent_cfg, "_node_id": "eval"})
    urls = [r["doc_url"] for r in (out.get("retrieval") or []) if r.get("doc_url")]
    tokens = ((out.get("trace") or [{}])[0].get("data") or {}).get("tokens") or {}
    return urls, out.get("agent_iterations", 1), int(tokens.get("total") or 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--flow", default=ACME_SUPPORT)
    args = ap.parse_args()

    flow = load_flow(flow_id=args.flow)
    agent_cfg = _agent_config(flow)
    retrieve_cfg = dict(agent_cfg.get("retrieve") or {})
    tenant_id = flow["tenant_id"]

    qrels = load_qrels("hard")
    print(f"flow: {flow['name']} v{flow['version']}  ·  agent config: {agent_cfg}\n"
          f"{len(qrels)} hard questions\n")

    baseline_ranked, agent_ranked = [], []
    iterations, tokens_total, reformulated = [], 0, 0
    for q in qrels:
        b = run_baseline(q, tenant_id, retrieve_cfg)
        a, n_iter, toks = run_agent(q, tenant_id, agent_cfg)
        baseline_ranked.append(b)
        agent_ranked.append(a)
        iterations.append(n_iter)
        tokens_total += toks
        reformulated += n_iter > 1
        print(f"  [{q['id']}] baseline_top={b[0] if b else '-'!r:<55} "
              f"agent_top={a[0] if a else '-'!r:<55} iterations={n_iter}")

    score(baseline_ranked, qrels, "baseline (single retrieve)")
    score(agent_ranked, qrels, "agent (multi-hop, real live Acme config)")

    n = len(qrels)
    print(f"\ncost/behavior — mean iterations {sum(iterations) / n:.2f}, "
          f"{reformulated}/{n} questions reformulated at least once, "
          f"{tokens_total} tokens spent on the agent side "
          f"(baseline side: 0 -- retrieval only, no LLM call)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
