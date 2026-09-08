"""
One-off: import UrbanPiper's exported cases into the gunner tenant's
`case_memory` (thin CLI over interpreter.case_import).

Put the Salesforce Bulk-API XML export(s) in ./gunner urbanpiper old cases/
(an EmailMessage query result, and optionally a Case query result).

    python -m scripts.import_gunner_cases            # import
    python -m scripts.import_gunner_cases --dry-run  # parse + report only
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from ingestion import case_memory_sync as cms  # noqa: E402
from interpreter import case_import  # noqa: E402

TENANT = "ee4102db-ab47-4deb-b28b-9efc5a02575b"  # gunner
DIR = pathlib.Path(__file__).resolve().parents[1] / "gunner urbanpiper old cases"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    email_xml = case_xml = None
    for f in sorted(DIR.glob("*.xml")):
        data = f.read_bytes()
        kind = case_import.sniff(data)
        if kind == "sf_email":
            email_xml = data
        elif kind == "sf_case":
            case_xml = data
    if not email_xml:
        sys.exit(f"no EmailMessage export found in {DIR}")

    rows, stats = case_import.parse_salesforce_bulk(email_xml, case_xml, tenant_id=TENANT)
    print("stats:", stats)
    for r in rows[:5]:
        print(f"\n  [{r['case_number']}] {r['subject']}")
        print(f"    problem   : {(r['body_summary'] or '')[:120]!r}")
        print(f"    resolution: {(r['resolution_text'] or '')[:160]!r}")
        print(f"    kind={r['resolution_kind']} generalizable={r['generalizable']}")
    if not rows:
        return
    n = cms._sync_rows(rows, dry=args.dry_run)
    print(f"\n{'[dry-run] would sync' if args.dry_run else 'synced'} {n} case_memory row(s) "
          f"for tenant {TENANT}")


if __name__ == "__main__":
    main()
