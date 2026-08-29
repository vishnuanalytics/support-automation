"""
Phase 4 integration check — one interpreter, three flows, three tenants/teams.

Needs Supabase (.env) and downloads the embed + rerank models on first run;
it hits real retrieval, so it's slow (~30-60s) and lives apart from the
offline suite. Skips cleanly if SUPABASE_URL is unset.

    python -m tests.test_multiflow

Proof: the SAME case JSON, run through each published flow, ends in a
different terminal purely because the flow rows differ:

  Acme     / support      -> auto_reply   (lenient per-tier gate)
  Globex   / support      -> ask_human    (strict gate, nothing auto-sends)
  Acme     / offboarding  -> handover     (no gate node at all)
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

# deterministic + no external writes: force the stub LLM and SF dry-run
for _k in ("GROQ_API_KEY", "SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN",
           "SF_CONSUMER_KEY", "SF_CONSUMER_SECRET", "SF_PRIVATE_KEY", "SF_PRIVATE_KEY_FILE"):
    os.environ.pop(_k, None)

from interpreter.builder import build_graph  # noqa: E402
from interpreter.loader import list_flows, load_flow  # noqa: E402

ACME = "00000000-0000-0000-0000-000000000000"
GLOBEX = "22222222-2222-2222-2222-222222222222"

CASE = json.loads(
    (pathlib.Path(__file__).resolve().parents[1] / "interpreter" / "cases" / "basic_howto.json").read_text()
)

EXPECT = [
    (ACME, "support", "auto_reply"),
    (GLOBEX, "support", "ask_human"),
    (ACME, "offboarding", "handover"),
]


def main() -> int:
    if not os.environ.get("SUPABASE_URL"):
        print("SKIP: no SUPABASE_URL in env")
        return 0

    published = {(f["tenant_id"], f["team"]) for f in list_flows(status="published")}
    print(f"published flows: {sorted(published)}\n")

    ok = True
    for tenant, team, want in EXPECT:
        assert (tenant, team) in published, f"missing published flow for {tenant}/{team}"
        flow = load_flow(tenant_id=tenant, team=team, status="published")
        final = build_graph(flow).invoke({"case": dict(CASE), "trace": []})
        got = (final.get("outcome") or {}).get("action")
        node_types = [n["type"] for n in flow["nodes"]]
        line = "ok  " if got == want else "FAIL"
        ok &= got == want
        print(f"  {line} {tenant[:8]}/{team:<11} {len(flow['nodes'])} nodes "
              f"({'+gate' if 'confidence_gate' in node_types else 'no gate'}) -> {got}  (want {want})")

    # the multi-tenant point: identical input, the two 'support' flows diverge
    a = build_graph(load_flow(tenant_id=ACME, team="support")).invoke({"case": dict(CASE), "trace": []})
    b = build_graph(load_flow(tenant_id=GLOBEX, team="support")).invoke({"case": dict(CASE), "trace": []})
    same_case_differs = a["outcome"]["action"] != b["outcome"]["action"]
    print(f"\n  {'ok  ' if same_case_differs else 'FAIL'} same case, Acme vs Globex support "
          f"-> {a['outcome']['action']} vs {b['outcome']['action']} "
          f"(gate {a['confidence_gate']['score']} vs threshold "
          f"{a['confidence_gate']['threshold']} / {b['confidence_gate']['threshold']})")
    ok &= same_case_differs

    print(f"\n{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
