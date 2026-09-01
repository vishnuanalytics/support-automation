"""
Phase 27g — the Case backstops, via a metadata deploy.

  * validation rule `Close_Needs_Type` — a Case can't be Closed without a
    `Type` and a non-blank `Description` (keeps the citable resolution set
    clean).
  * list views `Live_Queue` (open Cases, sorted by Next_Action_Due__c) and
    `SLA_Breach` (SLA_Breach__c = true).

    python scripts/sf_backstops.py --dry-run
    python scripts/sf_backstops.py
    python scripts/sf_backstops.py --remove

The native time-based Case Escalation Rule from the design is intentionally
skipped — the app `queue_sweep` acts at 30 min and is the primary path; add
the native rule by hand later if you want a worker-outage backstop.
"""

from __future__ import annotations

import argparse
import io
import pathlib
import sys
import tempfile
import time
import zipfile

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from interpreter.salesforce import _client, available  # noqa: E402

API = "59.0"

PACKAGE = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<Package xmlns="http://soap.sforce.com/2006/04/metadata">\n'
           '  <types><members>Case.Close_Needs_Type</members>'
           '<name>ValidationRule</name></types>\n'
           '  <types><members>Case.Live_Queue</members>'
           '<members>Case.SLA_Breach</members><name>ListView</name></types>\n'
           f'  <version>{API}</version>\n</Package>\n')

EMPTY_PACKAGE = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<Package xmlns="http://soap.sforce.com/2006/04/metadata">'
                 f'<version>{API}</version></Package>\n')

DESTRUCTIVE = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<Package xmlns="http://soap.sforce.com/2006/04/metadata">\n'
               '  <types><members>Case.Close_Needs_Type</members>'
               '<name>ValidationRule</name></types>\n'
               '  <types><members>Case.Live_Queue</members>'
               '<members>Case.SLA_Breach</members><name>ListView</name></types>\n'
               f'  <version>{API}</version>\n</Package>\n')

VALIDATION_RULE = """\
<?xml version="1.0" encoding="UTF-8"?>
<ValidationRule xmlns="http://soap.sforce.com/2006/04/metadata">
    <fullName>Close_Needs_Type</fullName>
    <active>true</active>
    <description>Phase 27g — a Case can't be Closed without a Type and a resolution summary.</description>
    <errorConditionFormula>AND(
  ISPICKVAL(Status, &quot;Closed&quot;),
  OR(ISBLANK(TEXT(Type)), ISBLANK(Description))
)</errorConditionFormula>
    <errorMessage>Set a Case Type and a resolution summary (Description) before closing.</errorMessage>
</ValidationRule>
"""

LIVE_QUEUE = """\
<?xml version="1.0" encoding="UTF-8"?>
<ListView xmlns="http://soap.sforce.com/2006/04/metadata">
    <fullName>Live_Queue</fullName>
    <label>Live Queue</label>
    <filterScope>Everything</filterScope>
    <filters><field>Case.IsClosed</field><operation>equals</operation><value>false</value></filters>
    <columns>CASE.CASE_NUMBER</columns>
    <columns>SUBJECT</columns>
    <columns>STATUS</columns>
    <columns>Case.Routed_Team__c</columns>
    <columns>Case.Next_Action__c</columns>
    <columns>Case.Next_Action_Due__c</columns>
    <columns>Case.AI_Confidence__c</columns>
    <columns>CASE.OWNER_NAME</columns>
</ListView>
"""

SLA_BREACH = """\
<?xml version="1.0" encoding="UTF-8"?>
<ListView xmlns="http://soap.sforce.com/2006/04/metadata">
    <fullName>SLA_Breach</fullName>
    <label>SLA Breach</label>
    <filterScope>Everything</filterScope>
    <filters><field>Case.SLA_Breach__c</field><operation>equals</operation><value>true</value></filters>
    <filters><field>Case.IsClosed</field><operation>equals</operation><value>false</value></filters>
    <columns>CASE.CASE_NUMBER</columns>
    <columns>SUBJECT</columns>
    <columns>STATUS</columns>
    <columns>Case.Routed_Team__c</columns>
    <columns>Case.Escalation_Reason__c</columns>
    <columns>Case.Next_Action_Due__c</columns>
    <columns>CASE.OWNER_NAME</columns>
</ListView>
"""


def _deploy(sf, zbytes: bytes, label: str) -> int:
    zp = pathlib.Path(tempfile.gettempdir()) / "sf_backstops.zip"
    zp.write_bytes(zbytes)
    print(f"deploying: {label} …")
    dep = sf.deploy(str(zp), sandbox=False, testLevel="NoTestRun")
    aid = dep["asyncId"] if isinstance(dep, dict) else dep[0]
    res: dict = {}
    for _ in range(40):
        time.sleep(3)
        res = sf.checkDeployStatus(aid, include_details=True)
        if (res or {}).get("state") not in (None, "", "InProgress", "Pending", "Queued"):
            break
    print(f"deploy: state={res.get('state')}")
    ok = res.get("state") in ("Succeeded", "Completed")
    if not ok:
        for f in (res.get("deployment_detail", {}) or {}).get("componentFailures", []) or []:
            print("  FAIL", f.get("fullName"), "-", f.get("problem"))
    return 0 if ok else 1


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("package.xml", PACKAGE)
        z.writestr("objects/Case/validationRules/Close_Needs_Type.validationRule", VALIDATION_RULE)
        z.writestr("objects/Case/listViews/Live_Queue.listView", LIVE_QUEUE)
        z.writestr("objects/Case/listViews/SLA_Breach.listView", SLA_BREACH)
    return buf.getvalue()


def _remove_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("package.xml", EMPTY_PACKAGE)
        z.writestr("destructiveChanges.xml", DESTRUCTIVE)
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(prog="scripts.sf_backstops")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--remove", action="store_true")
    args = ap.parse_args()
    if not available():
        sys.exit("no SF creds in .env")
    if args.dry_run:
        print("[dry-run] WOULD deploy:")
        print("  ValidationRule Case.Close_Needs_Type (Closed needs Type + Description)")
        print("  ListView Case.Live_Queue, Case.SLA_Breach")
        return 0
    sf = _client()
    return _deploy(sf, _remove_zip() if args.remove else _zip(),
                   "REMOVE backstops" if args.remove else "27g backstops")


if __name__ == "__main__":
    raise SystemExit(main())
