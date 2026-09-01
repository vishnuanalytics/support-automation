"""
Phase 27g — the Case backstop that's worth scripting: the `Close_Needs_Type`
validation rule (a Case can't be Closed without a `Type` and a non-blank
`Description`).

    python scripts/sf_backstops.py --dry-run
    python scripts/sf_backstops.py
    python scripts/sf_backstops.py --remove

The two list views (Live Queue / SLA Breach) and the native time-based Case
Escalation Rule are a 60-second Setup task each — see
docs/CASE_CONTROL_PLANE_SF.md.
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
           f'  <version>{API}</version>\n</Package>\n')

EMPTY_PACKAGE = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<Package xmlns="http://soap.sforce.com/2006/04/metadata">'
                 f'<version>{API}</version></Package>\n')

DESTRUCTIVE = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<Package xmlns="http://soap.sforce.com/2006/04/metadata">\n'
               '  <types><members>Case.Close_Needs_Type</members>'
               '<name>ValidationRule</name></types>\n'
               f'  <version>{API}</version>\n</Package>\n')

# Classic MDAPI: a validation rule lives inside the object file. A partial
# Case.object deploy merges it without touching anything else on the object.
CASE_OBJECT = """\
<?xml version="1.0" encoding="UTF-8"?>
<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">
    <validationRules>
        <fullName>Close_Needs_Type</fullName>
        <active>true</active>
        <description>Phase 27g - a Case can't be Closed without a Type and a resolution summary.</description>
        <errorConditionFormula>AND(ISPICKVAL(Status, &quot;Closed&quot;), OR(ISBLANK(TEXT(Type)), ISBLANK(Description)))</errorConditionFormula>
        <errorMessage>Set a Case Type and a resolution summary (Description) before closing.</errorMessage>
    </validationRules>
</CustomObject>
"""


def _deploy(sf, zbytes: bytes, label: str) -> int:
    zp = pathlib.Path(tempfile.gettempdir()) / "sf_backstops.zip"
    zp.write_bytes(zbytes)
    print(f"deploying: {label} ...")
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
        for e in (res.get("deployment_detail", {}) or {}).get("errors", []) or []:
            print("  ERR", e.get("message"))
    return 0 if ok else 1


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("package.xml", PACKAGE)
        z.writestr("objects/Case.object", CASE_OBJECT)
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
        print("[dry-run] WOULD deploy ValidationRule Case.Close_Needs_Type")
        return 0
    sf = _client()
    return _deploy(sf, _remove_zip() if args.remove else _zip(),
                   "REMOVE Close_Needs_Type" if args.remove else "27g Close_Needs_Type")


if __name__ == "__main__":
    raise SystemExit(main())
