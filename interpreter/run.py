"""
CLI: load a flow from Supabase, compile it, run a support case through it.

    python -m interpreter.run --flow 11111111-1111-1111-1111-111111111111 \
        --case interpreter/cases/basic_howto.json

    python -m interpreter.run --tenant 00000000-0000-0000-0000-000000000000 \
        --team support --status draft --case interpreter/cases/enterprise_bug.json

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
    ap.add_argument("--case", help="path to a case JSON file (may carry an \"sf_id\")")
    ap.add_argument("--sf-case", dest="sf_case", metavar="ID",
                    help="pull this Salesforce Case Id and run it (needs SF creds in .env)")
    ap.add_argument("--describe", action="store_true", help="print wiring and exit")
    ap.add_argument("--list", action="store_true", help="list flows (per tenant/team) and exit")
    ap.add_argument("--no-record", action="store_true", help="don't persist this run to the runs table")
    ap.add_argument("--json", action="store_true", help="emit final state as JSON")
    args = ap.parse_args(argv)

    if args.list:
        from .loader import list_flows
        rows = list_flows(tenant_id=args.tenant)
        cur = None
        for r in rows:
            if r["tenant_id"] != cur:
                cur = r["tenant_id"]
                print(f"\ntenant {cur}")
            print(f"  {r['team']:<12} {r['status']:<10} v{r['version']}  {r['name']}")
            print(f"  {'':<12} {'':<10} {r['flow_id']}")
        return 0

    if not args.flow and not (args.tenant and args.team):
        ap.error("pass --flow, or both --tenant and --team")

    flow = load_flow(
        flow_id=args.flow, tenant_id=args.tenant, team=args.team, status=args.status
    )

    if args.describe:
        print(describe_graph(flow))
        return 0

    graph = build_graph(flow)
    if args.sf_case:
        from .salesforce import get_case
        case = get_case(args.sf_case)
    else:
        case = _load_case(args.case)

    print(describe_graph(flow))
    print(f"\nrunning case {case.get('case_id', '?')}: {case.get('subject', '')!r}\n")

    final = graph.invoke({"case": case, "trace": []})

    if not args.no_record:
        from .runs import record_run
        rid = record_run(flow, final, case=case, source="cli")
        if rid:
            print(f"recorded run {rid}\n")

    for i, step in enumerate(final.get("trace", []), 1):
        print(f"  {i}. [{step['type']}] {step['summary']}")

    sfw = final.get("sf_writeback")
    if sfw:
        if not sfw.get("target"):
            print(f"\nsalesforce: skipped ({sfw.get('status', 'no target')}); "
                  f"planned={sfw.get('planned') or {}}")
        elif sfw.get("dry_run"):
            print(f"\nsalesforce [dry-run]: Case {sfw.get('target')} "
                  f"would write {sfw.get('planned') or {}}")
        else:
            print(f"\nsalesforce [live]: Case {sfw.get('target')} "
                  f"written={sfw.get('written') or {}} "
                  f"skipped={list(sfw.get('skipped') or {})}")

    outcome = final.get("outcome", {})
    print(f"\noutcome: {outcome.get('action', '(none)')}")
    if outcome.get("action") in {"auto_reply", "ask_human", "handover"}:
        print(f"  tier={final.get('tier')}  confidence={final.get('confidence')}")
        if outcome.get("chatter"):
            c = outcome["chatter"]
            print(f"  chatter: {'dry-run' if c.get('dry_run') else 'posted'} "
                  f"mention={c.get('mention_id')} id={c.get('feed_element_id')}")
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
