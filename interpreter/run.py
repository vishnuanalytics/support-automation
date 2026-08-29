"""
CLI: load a flow from Supabase, compile it, run a support case through it.

    python -m interpreter.run --flow 11111111-1111-1111-1111-111111111111 \
        --case cases/basic_howto.json

    python -m interpreter.run --tenant 00000000-0000-0000-0000-000000000000 \
        --team support --status draft --case cases/enterprise_bug.json

    python -m interpreter.run --flow <id> --describe        # just print the wiring

With no --case, a small built-in sample case is used. With no GROQ_API_KEY
set, llm.py returns deterministic stubs so the whole run still completes.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from dotenv import load_dotenv

from .builder import build_graph, describe_graph
from .loader import load_flow

load_dotenv()

_SAMPLE_CASE = {
    "case_id": "DEMO-1",
    "subject": "How do I set up a webhook trigger?",
    "body": "I want my Zap to start when my app sends a POST request. "
            "Where do I configure the webhook URL and how do I test it?",
    "account": {"name": "Acme Co", "customer_type": "premium", "region": "EMEA"},
    "contact": {"name": "Dana Lee", "email": "dana@acme.example"},
}


def _load_case(path: str | None) -> dict:
    if not path:
        return dict(_SAMPLE_CASE)
    return json.loads(pathlib.Path(path).read_text())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="interpreter.run")
    ap.add_argument("--flow", help="flow_id (uuid)")
    ap.add_argument("--tenant", help="tenant_id (with --team)")
    ap.add_argument("--team", help="team (with --tenant)")
    ap.add_argument("--status", default="published", help="flow status when selecting by team")
    ap.add_argument("--case", help="path to a case JSON file")
    ap.add_argument("--describe", action="store_true", help="print wiring and exit")
    ap.add_argument("--json", action="store_true", help="emit final state as JSON")
    args = ap.parse_args(argv)

    if not args.flow and not (args.tenant and args.team):
        ap.error("pass --flow, or both --tenant and --team")

    flow = load_flow(
        flow_id=args.flow, tenant_id=args.tenant, team=args.team, status=args.status
    )

    if args.describe:
        print(describe_graph(flow))
        return 0

    graph = build_graph(flow)
    case = _load_case(args.case)

    print(describe_graph(flow))
    print(f"\nrunning case {case.get('case_id', '?')}: {case.get('subject', '')!r}\n")

    final = graph.invoke({"case": case, "trace": []})

    for i, step in enumerate(final.get("trace", []), 1):
        print(f"  {i}. [{step['type']}] {step['summary']}")

    outcome = final.get("outcome", {})
    print(f"\noutcome: {outcome.get('action', '(none)')}")
    if outcome.get("action") in {"auto_reply", "ask_human", "handover"}:
        print(f"  tier={final.get('tier')}  confidence={final.get('confidence')}")
        draft = outcome.get("reply") or outcome.get("draft") or ""
        if draft:
            print("  draft:")
            for line in draft.splitlines():
                print(f"    {line}")

    if args.json:
        print("\n--- final state ---")
        print(json.dumps(final, indent=2, default=str))

    return 0


if __name__ == "__main__":
    sys.exit(main())
