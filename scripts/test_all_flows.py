"""
End-to-end smoke test for every published flow, against live Salesforce +
the real LLM. For each (flow, scenario) it creates a real Case (Account
carries the tier), runs the published snapshot on it, and reports the
outcome + what landed on the Case (fields, owner queue). Cleans up every
record it made unless --keep.

    python scripts/test_all_flows.py                 # all flows, all scenarios
    python scripts/test_all_flows.py --flow 11111111-1111-1111-1111-111111111111
    python scripts/test_all_flows.py --keep          # leave the Cases in SF

task_dispatch flows (offboarding / support-approvals) are run with a
NON-triggering case so nothing posts to Slack/GitHub.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from interpreter import salesforce as sfmod  # noqa: E402
from interpreter.builder import build_graph  # noqa: E402
from interpreter.loader import list_flows, load_flow  # noqa: E402

# scenario: (tier, subject, body, expected_action)  — expected is a hint, not asserted hard
ANSWERABLE = ("basic", "How do I test a Zap trigger before turning it on?",
              "I built a Zap with a trigger and two actions. How do I test the trigger "
              "step to check the sample data before switching the Zap on?", "auto_reply")
BILLING = ("premium", "Refund for a duplicate annual charge",
           "We were billed twice for our annual plan this month. Please refund the "
           "duplicate charge to the card on file.", "ask_human")
ENTERPRISE = ("enterprise", "SSO login broken for our whole org after a domain change",
              "We changed our email domain and now nobody can log in via SSO. ~120 "
              "users blocked, this is urgent.", "handover")
VAGUE = ("basic", "need help with the sap model thing",
         "hi team, the sap model isn't working for me, can you help", "need_info/ask_human")

SCENARIOS: dict[str, list[tuple]] = {
    # tier-gated support flows: all three branches
    "11111111-1111-1111-1111-111111111111": [ANSWERABLE, BILLING, ENTERPRISE],
    "e5e5e5e5-5555-4555-8555-555555555555": [ANSWERABLE, BILLING, ENTERPRISE],
    "a2a2a2a2-2222-4222-8222-222222222222": [ANSWERABLE, BILLING, ENTERPRISE],
    "d4d4d4d4-4444-4444-8444-444444444444": [ANSWERABLE, BILLING, ENTERPRISE, VAGUE],
    # no tier gate / no SF write
    "a4f1e382-403c-452e-9a3f-2f4ac4442bd6": [ANSWERABLE, BILLING],
    # policy_gate + task_dispatch — ANSWERABLE only, so the policy rule
    # doesn't match and nothing posts to Slack / opens a GitHub issue.
    "c3c3c3c3-3333-4333-8333-333333333333": [ANSWERABLE],
    "781cf1cc-2750-4d4d-8e0b-3a7d9ca7117c": [ANSWERABLE],
}


def _mk_case(sf, tier: str, subject: str, body: str, ts: int) -> dict:
    acc = sf.Account.create({"Name": f"FlowTest {tier} {ts}", "Tier__c": tier})
    con = sf.Contact.create({"LastName": f"{tier.title()} Tester",
                             "Email": f"{tier}.{ts}@flowtest.example", "AccountId": acc["id"]})
    case = sf.Case.create({"Subject": subject, "Description": body, "Origin": "Web",
                           "Status": "New", "AccountId": acc["id"], "ContactId": con["id"]})
    return {"case_id": case["id"], "account_id": acc["id"], "contact_id": con["id"]}


def _run_one(flow: dict, g, sf, tier, subject, body, expected, ts) -> dict:
    ids = _mk_case(sf, tier, subject, body, ts)
    case = sfmod.get_case(ids["case_id"])
    t0 = time.time()
    final = g.invoke({"case": case, "tenant_id": flow["tenant_id"],
                      "team": flow.get("team"), "trace": []})
    dt = time.time() - t0
    action = (final.get("outcome") or {}).get("action")
    row = sf.query(
        "SELECT Priority, Module__c, SubModule__c, Region__c, Topic__c, "
        "Owner.Name, Owner.Type FROM Case WHERE Id = '%s'" % ids["case_id"]
    )["records"][0]
    owner = (row.get("Owner") or {})
    stub = bool((final.get("classification") or {}).get("stub"))
    return {
        "ids": ids, "action": action, "expected": expected, "secs": round(dt, 1),
        "stub": stub,
        "module": row.get("Module__c"), "submodule": row.get("SubModule__c"),
        "region": row.get("Region__c"), "topic": row.get("Topic__c"),
        "priority": row.get("Priority"),
        "owner": f"{owner.get('Name')} ({owner.get('Type')})" if owner.get("Type") == "Queue" else "-",
        "trace": [t["type"] for t in final.get("trace", [])],
    }


def _cleanup(sf, made: list[dict]) -> None:
    for kind in ("case_id", "contact_id", "account_id"):
        for m in made:
            if m.get(kind):
                try:
                    getattr(sf, {"case_id": "Case", "contact_id": "Contact",
                                 "account_id": "Account"}[kind]).delete(m[kind])
                except Exception as e:  # noqa: BLE001
                    print(f"  cleanup {kind} {m[kind]}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--flow", help="only this flow_id")
    ap.add_argument("--keep", action="store_true", help="don't delete the Cases")
    args = ap.parse_args()
    if not sfmod.available():
        sys.exit("no SF creds in .env")
    sf = sfmod._client()

    flows = {f["flow_id"]: f for f in list_flows(status="published")}
    targets = [args.flow] if args.flow else list(SCENARIOS)
    made: list[dict] = []
    print(f"{'flow':40} {'scenario':10} {'->':>14}  {'want':16} {'queue':26} module/sub / topic")
    print("-" * 130)
    for fid in targets:
        meta = flows.get(fid)
        if not meta:
            print(f"{fid}: not a published flow — skip")
            continue
        flow = load_flow(flow_id=fid, status="published")
        g = build_graph(flow)
        for tier, subj, body, expected in SCENARIOS.get(fid, [ANSWERABLE]):
            ts = int(time.time() * 1000) % 10_000_000
            try:
                r = _run_one(flow, g, sf, tier, subj, body, expected, ts)
            except Exception as e:  # noqa: BLE001
                print(f"{meta['name'][:40]:40} {tier:10}  ERROR: {e}")
                continue
            made.append(r["ids"])
            ok = "ok" if (r["expected"].split("/")[0] in (r["action"] or "")) or \
                         (r["action"] in r["expected"]) else "??"
            tag = " [stub]" if r["stub"] else ""
            print(f"{meta['name'][:40]:40} {tier:10} {r['action'] or '-':>14}  "
                  f"{r['expected']:16} {r['owner']:26} "
                  f"{r['module']}/{r['submodule']} / {r['topic']}  {r['secs']}s {ok}{tag}")
    if not args.keep:
        print("\ncleaning up...")
        _cleanup(sf, made)
        print(f"deleted {len(made)} test Case(s) + their Contacts/Accounts")
    else:
        print(f"\n--keep: {len(made)} Cases left in Salesforce")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
