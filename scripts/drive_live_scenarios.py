"""
Enqueue real `run_flow` jobs for a spread of scenarios — each creates a real
Salesforce Case through the published sf_entry flow, so we can watch the
worker + inspect the Cases in Salesforce.

    python scripts/drive_live_scenarios.py            # enqueue all
    python scripts/drive_live_scenarios.py --only A,D  # just those
    python scripts/drive_live_scenarios.py --list      # print, enqueue nothing

Senders map to existing SF tier accounts:
  sam@indie.example    -> Indie Dev Co       (basic)
  priya@northwind.example -> Northwind Ltd    (premium / EMEA)
  alex@globex.example  -> Globex Enterprise   (enterprise / NA)
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

FLOW_ID = "e5e5e5e5-5555-4555-8555-555555555555"

# key, sender, subject, body, expected outcome / where it lands
SCENARIOS = {
    "A": ("sam@indie.example", "How do I turn on a Zap",
          "Hi, I built a Zap but it shows as OFF in my dashboard. How do I turn it on / publish it?",
          "auto_reply"),
    "B": ("sam@indie.example", "Charged twice this month",
          "You charged my card twice for the annual plan this month. I want a refund for one of the charges.",
          "notify -> Billing (Case stays in Team_Email)"),
    "C": ("sam@indie.example", "Locked out - SSO not working",
          "None of our team can log in. Our SSO / SAML login with Okta stopped working this morning.",
          "notify -> Login/identity (Case stays in Team_Email)"),
    "D": ("priya@northwind.example", "Renew contract and add seats",
          "We'd like to renew our annual contract early and add 10 more seats. Who is our account manager?",
          "ask_human -> Team_CSM (reassigned)"),
    "E": ("priya@northwind.example", "Cancel our account",
          "Please cancel our account at end of term and export all of our data (GDPR right-to-erasure request).",
          "handover -> Team_Offboarding (reassigned)"),
    "F": ("alex@globex.example", "Scheduled trigger timezones",
          "How do scheduled triggers handle daylight-saving / timezone changes for a daily 9am run?",
          "handover -> Enterprise_Support (reassigned)"),
    "G": ("sam@indie.example", "it's broken",
          "nothing is working right now, please help asap",
          "clarify (ask the customer; Case stays in Team_Email)"),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--go", action="store_true",
                    help="actually enqueue (creates real SF Cases + spends LLM quota); "
                         "without it this just prints what it would do")
    args = ap.parse_args()

    keys = [k.strip().upper() for k in args.only.split(",") if k.strip()] or list(SCENARIOS)

    if args.list or not args.go:
        if not args.go:
            print("(dry — pass --go to actually enqueue; this spends LLM quota + "
                  "creates real Salesforce Cases)\n")
        for k in keys:
            s, subj, body, exp = SCENARIOS[k]
            print(f"{k}  {s:<26} {subj!r}  -> {exp}")
        return 0

    from ingestion.scraper import get_supabase
    from interpreter import jobs

    sb = get_supabase()
    stamp = int(time.time())
    print(f"enqueuing {len(keys)} run_flow job(s) against {FLOW_ID}\n")
    for k in keys:
        sender, subj, body, exp = SCENARIOS[k]
        mid = f"livescen-{k}-{stamp}"
        case = {
            "case_id": mid,
            "subject": subj,
            "body": body,
            "from": sender,
            "channel": "email",
            "message_id": f"<{mid}@test.local>",
        }
        jid = jobs.enqueue(
            "run_flow",
            {"flow_id": FLOW_ID, "case": case, "idempotency_key": mid},
            dedupe_key=f"email:{mid}", sb=sb,
        )
        print(f"  {k}  {sender:<26} {subj!r:<38} job={jid}  (expect: {exp})")

    print("\nwatch:  docker compose logs -f worker")
    print("then:   python scripts/report_live_scenarios.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
