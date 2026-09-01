"""
Phase 27f — the one-entry Case assignment rule.

Backs up the current active `Standard` Case assignment rule, then replaces
its entries with a single catch-all that assigns every new Case to the
`AI_Intake` queue. The pipeline drives from there (27c) and escalations
route via Omni-Channel (27b).

    python scripts/sf_assignment_cutover.py --dry-run
    python scripts/sf_assignment_cutover.py            # cut over (backup first)
    python scripts/sf_assignment_cutover.py --restore  # redeploy the backup

Reversible: the backup XML is written to
`scripts/_assignment_backup/Case.assignmentRules-meta.xml`.
"""

from __future__ import annotations

import argparse
import io
import pathlib
import sys
import time
import zipfile

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from interpreter.salesforce import _client, available  # noqa: E402

API = "59.0"
BACKUP_DIR = pathlib.Path(__file__).resolve().parent / "_assignment_backup"
BACKUP = BACKUP_DIR / "Case.assignmentRules.json"

PACKAGE = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<Package xmlns="http://soap.sforce.com/2006/04/metadata">\n'
           '  <types><members>Case</members><name>AssignmentRules</name></types>\n'
           f'  <version>{API}</version>\n</Package>\n')

CUTOVER_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<AssignmentRules xmlns="http://soap.sforce.com/2006/04/metadata">
    <assignmentRule>
        <fullName>Standard</fullName>
        <active>true</active>
        <ruleEntry>
            <assignedTo>AI_Intake</assignedTo>
            <assignedToType>Queue</assignedToType>
            <formula>true</formula>
        </ruleEntry>
    </assignmentRule>
</AssignmentRules>
"""


def _read_current(sf) -> dict:
    """The live Case assignment rules, as a plain dict (for the backup + to
    rebuild the exact XML on --restore)."""
    ar = sf.mdapi.AssignmentRules.read("Case")

    def _plain(o):
        if isinstance(o, (str, int, float, bool)) or o is None:
            return o
        if isinstance(o, (list, tuple)):
            return [_plain(x) for x in o]
        return {k: _plain(getattr(o, k)) for k in getattr(o, "__values__", {})}

    return _plain(ar)


def _xml_from_backup(data: dict) -> str:
    """Rebuild an AssignmentRules XML from the backup dict (--restore)."""
    import xml.sax.saxutils as sx

    def esc(v):
        return sx.escape(str(v))

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<AssignmentRules xmlns="http://soap.sforce.com/2006/04/metadata">']
    for rule in data.get("assignmentRule") or []:
        out.append("  <assignmentRule>")
        out.append(f"    <fullName>{esc(rule.get('fullName'))}</fullName>")
        out.append(f"    <active>{'true' if rule.get('active') else 'false'}</active>")
        for e in rule.get("ruleEntry") or []:
            out.append("    <ruleEntry>")
            if e.get("assignedTo"):
                out.append(f"      <assignedTo>{esc(e['assignedTo'])}</assignedTo>")
            if e.get("assignedToType"):
                out.append(f"      <assignedToType>{esc(e['assignedToType'])}</assignedToType>")
            if e.get("formula"):
                out.append(f"      <formula>{esc(e['formula'])}</formula>")
            for ci in e.get("criteriaItems") or []:
                out.append("      <criteriaItems>")
                out.append(f"        <field>{esc(ci.get('field'))}</field>")
                out.append(f"        <operation>{esc(ci.get('operation'))}</operation>")
                if ci.get("value") is not None:
                    out.append(f"        <value>{esc(ci['value'])}</value>")
                out.append("      </criteriaItems>")
            out.append("    </ruleEntry>")
        out.append("  </assignmentRule>")
    out.append("</AssignmentRules>\n")
    return "\n".join(out)


def _deploy_xml(sf, xml: str, label: str) -> int:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("package.xml", PACKAGE)
        z.writestr("assignmentRules/Case.assignmentRules", xml)
    import tempfile
    zp = pathlib.Path(tempfile.gettempdir()) / "sf_assign.zip"
    zp.write_bytes(buf.getvalue())
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


def main() -> int:
    ap = argparse.ArgumentParser(prog="scripts.sf_assignment_cutover")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", action="store_true")
    args = ap.parse_args()
    if not available():
        sys.exit("no SF creds in .env")
    sf = _client()

    import json

    if args.restore:
        if not BACKUP.exists():
            sys.exit(f"no backup at {BACKUP}")
        data = json.loads(BACKUP.read_text())
        return _deploy_xml(sf, _xml_from_backup(data), "RESTORE original Standard rule")

    current = _read_current(sf)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP.write_text(json.dumps(current, indent=1))
    entries = sum(len(r.get("ruleEntry") or []) for r in current.get("assignmentRule") or [])
    print(f"backup written: {BACKUP}")
    print(f"current Standard rule: {entries} rule entr{'y' if entries == 1 else 'ies'}")

    if args.dry_run:
        print("\n[dry-run] WOULD deploy the single catch-all -> AI_Intake entry:")
        print(CUTOVER_XML)
        return 0
    return _deploy_xml(sf, CUTOVER_XML, "cutover: Standard -> single entry -> AI_Intake")


if __name__ == "__main__":
    raise SystemExit(main())
