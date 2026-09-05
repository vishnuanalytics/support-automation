"""
One-shot Salesforce org setup for the support-automation flows: the routing
queues the `handover` / `ask_human` nodes point at, plus the Case picklists
`sf_writeback` fills.

    python scripts/sf_support_setup.py               # do everything
    python scripts/sf_support_setup.py --only queues
    python scripts/sf_support_setup.py --dry-run

Stages (each idempotent — re-running skips what already exists):
  queues      12 queues (5 per-team + 4 per-escalation-reason + 3 control-plane:
              AI_Intake / Unrouted_Review / SLA_Breach), Case assigned, the
              running user added as the sole member
  types       Case.Type  -> software-support values; Case.Status += Triaged /
              In Progress / Resolved / "Waiting on Customer"
  fields      Case.Module__c / Case.Region__c  Text -> restricted Picklist
              Case.SubModule__c  new Picklist, dependent on Module__c
              Case.Topic__c      new Text(255) — the classifier's raw slug
  cp_fields   Phase 27a case-control-plane fields: Routed_Team__c (picklist),
              Next_Action__c / Next_Action_Due__c, Escalation_Reason__c,
              AI_Confidence__c, Last_AI_Run_At__c, Last_Run_Id__c,
              Handoff_Slack_Ts__c, SLA_Breach__c
  fls         field-level security for the new/changed fields on the admin profile

Needs SF creds in .env and a user with "Customize Application" +
"Modify Metadata" (System Administrator has both).
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from interpreter.salesforce import _client, available  # noqa: E402

# ── config ──────────────────────────────────────────────────────────────
TEAM_QUEUES = [
    ("Team_Email", "Team — Email"),
    ("Team_Support", "Team — Support"),
    ("Team_CSM", "Team — CSM"),
    ("Team_Sales", "Team — Sales"),
    ("Team_Offboarding", "Team — Offboarding"),
]
REASON_QUEUES = [
    ("Support_L0L1", "Support L0/L1"),
    ("Billing_Escalations", "Billing Escalations"),
    ("Enterprise_Support", "Enterprise Support"),
    ("Support_Tier2", "Support Tier 2"),
]
# Phase 27a — the case-control-plane queues: one entry queue, a dead-letter
# for ambiguous classification, and a parking queue for missed SLA timers.
CONTROL_QUEUES = [
    ("AI_Intake", "AI Intake"),
    ("Unrouted_Review", "Unrouted Review"),
    ("SLA_Breach", "SLA Breach"),
]
ALL_QUEUES = TEAM_QUEUES + REASON_QUEUES + CONTROL_QUEUES

CASE_TYPE_VALUES = [
    "Question", "How-to", "Problem / Bug", "Billing",
    "Account / Login", "Feature Request", "Other",
]
CASE_STATUS_VALUES = [  # (label, closed)
    ("New", False), ("Triaged", False), ("In Progress", False),
    ("Working", False), ("Escalated", False), ("Waiting on Customer", False),
    ("Resolved", False), ("Closed", True),
]

MODULE_VALUES = [
    "Zaps", "Integrations & Apps", "Billing & Plans", "Account & Login",
    "API & Webhooks", "Data & Export", "Other",
]
REGION_VALUES = ["NA", "EMEA", "APAC", "LATAM", "Other"]
# sub-module -> controlling Module value
SUBMODULE_BY_MODULE = {
    "Zaps": ["Triggers", "Actions", "Filters", "Paths", "Scheduling"],
    "Integrations & Apps": ["Authentication", "App Errors", "New App Request"],
    "Billing & Plans": ["Charges", "Refunds", "Plan Change", "Invoices"],
    "Account & Login": ["SSO", "Password", "Two-Factor", "Members & Roles"],
    "API & Webhooks": ["REST API", "Webhooks", "Rate Limits"],
    "Data & Export": ["Export", "Retention", "Deletion / GDPR"],
    "Other": ["General"],
}
NEW_TEXT_FIELDS = [
    {"api": "Case.Topic__c", "label": "Topic (raw)", "length": 255},
]

# Phase 27a — the case-control-plane fields the pipeline + sweep read/write.
# `Routed_Team__c` values MUST match the app's `routed_team` strings exactly.
ROUTED_TEAM_VALUES = ["support", "tier2", "csm", "sales", "offboarding", "billing"]
CONTROL_PLANE_FIELDS = [
    {"api": "Case.Routed_Team__c", "label": "Routed Team", "type": "Picklist",
     "values": ROUTED_TEAM_VALUES},
    {"api": "Case.Next_Action__c", "label": "Next Action", "type": "Text", "length": 255},
    {"api": "Case.Next_Action_Due__c", "label": "Next Action Due", "type": "DateTime"},
    {"api": "Case.Escalation_Reason__c", "label": "Escalation Reason", "type": "Text", "length": 255},
    {"api": "Case.AI_Confidence__c", "label": "AI Confidence", "type": "Number",
     "precision": 3, "scale": 2},
    {"api": "Case.Last_AI_Run_At__c", "label": "Last AI Run At", "type": "DateTime"},
    {"api": "Case.Last_Run_Id__c", "label": "Last Run Id", "type": "Text", "length": 40},
    {"api": "Case.Handoff_Slack_Ts__c", "label": "Handoff Slack Ts", "type": "Text", "length": 64},
    {"api": "Case.SLA_Breach__c", "label": "SLA Breach", "type": "Checkbox"},
]
CONTROL_PLANE_FLS = [f["api"] for f in CONTROL_PLANE_FIELDS]
PICKLIST_CONVERT = [  # Text -> Picklist, restricted
    {"api": "Case.Module__c", "label": "Module", "values": MODULE_VALUES},
    {"api": "Case.Region__c", "label": "Region", "values": REGION_VALUES},
]
FLS_FIELDS = ["Case.Module__c", "Case.Region__c", "Case.SubModule__c",
              "Case.Topic__c"] + CONTROL_PLANE_FLS


# ── helpers ─────────────────────────────────────────────────────────────
def _existing_queue_names(sf) -> set[str]:
    return {r["DeveloperName"] for r in
            sf.query("SELECT DeveloperName FROM Group WHERE Type = 'Queue'")["records"]}


def _case_fields(sf) -> dict:
    return {f["name"]: f for f in sf.Case.describe()["fields"]}


def _cv(md, label: str, default: bool = False):
    return md.CustomValue(fullName=label, label=label, default=default)


def _create_ok(fn, *a) -> str:
    """Run an mdapi create; treat 'already exists' as success. Returns a note."""
    try:
        fn(*a)
        return "created"
    except Exception as e:  # noqa: BLE001
        s = str(e)
        if "DUPLICATE_DEVELOPER_NAME" in s or "DUPLICATE_VALUE" in s or "already" in s.lower():
            return "exists"
        raise


# ── stages ──────────────────────────────────────────────────────────────
def stage_queues(sf, dry: bool) -> None:
    me = __import__("os").environ["SF_USERNAME"]
    md = sf.mdapi
    have = _existing_queue_names(sf)
    for api, label in ALL_QUEUES:
        if api in have:
            print(f"  queue {api}: exists")
            continue
        if dry:
            print(f"  queue {api}: WOULD create ('{label}', Case, member={me})")
            continue
        md.Queue.create(md.Queue(
            fullName=api, name=label, doesSendEmailToMembers=False,
            queueSobject=[md.QueueSobject(sobjectType="Case")],
            queueMembers=md.QueueMembers(users=md.Users(user=[me])),
        ))
        print(f"  queue {api}: created")


def stage_types(sf, dry: bool) -> None:
    md = sf.mdapi
    if dry:
        print(f"  Case.Type  -> {CASE_TYPE_VALUES}")
        print(f"  Case.Status -> {[s for s, _ in CASE_STATUS_VALUES]}")
        return
    ct = md.StandardValueSet.read("CaseType")
    ct.standardValue = [
        md.StandardValue(fullName=v, label=v, default=(i == 0))
        for i, v in enumerate(CASE_TYPE_VALUES)
    ]
    md.StandardValueSet.update(ct)
    print(f"  Case.Type set to {CASE_TYPE_VALUES}")

    cs = md.StandardValueSet.read("CaseStatus")
    cs.standardValue = [
        md.StandardValue(fullName=label, label=label, default=(label == "New"), closed=closed)
        for label, closed in CASE_STATUS_VALUES
    ]
    md.StandardValueSet.update(cs)
    print(f"  Case.Status set to {[s for s, _ in CASE_STATUS_VALUES]}")


def stage_fields(sf, dry: bool) -> None:
    md = sf.mdapi
    have = _case_fields(sf)

    for f in NEW_TEXT_FIELDS:
        short = f["api"].split(".")[1]
        if short in have:
            print(f"  {f['api']}: exists")
        elif dry:
            print(f"  {f['api']}: WOULD create Text({f['length']})")
        else:
            note = _create_ok(md.CustomField.create, md.CustomField(
                fullName=f["api"], label=f["label"], type="Text",
                length=f["length"], required=False))
            print(f"  {f['api']}: {note} Text({f['length']})")

    for f in PICKLIST_CONVERT:
        short = f["api"].split(".")[1]
        cur = have.get(short, {})
        if cur.get("type") == "picklist":
            print(f"  {f['api']}: already a picklist")
            continue
        if dry:
            print(f"  {f['api']}: WOULD (re)create as restricted Picklist {f['values']}")
            continue
        vsd = md.ValueSetValuesDefinition(
            sorted=False, value=[_cv(md, v) for v in f["values"]])   # no default -> blank until set
        cf = md.CustomField(
            fullName=f["api"], label=f["label"], type="Picklist",
            valueSet=md.ValueSet(restricted=True, valueSetDefinition=vsd))
        if short in have and have[short].get("type") != "picklist":
            # Text -> Picklist isn't a supported in-place conversion; drop and
            # recreate (the classifier's slug is preserved in Topic__c anyway).
            try:
                md.CustomField.delete(f["api"])
                print(f"  {f['api']}: dropped Text field")
            except Exception as e:  # noqa: BLE001
                print(f"  {f['api']}: delete skipped ({str(e)[:60]})")
        print(f"  {f['api']}: {_create_ok(md.CustomField.create, cf)} restricted Picklist")

    # SubModule__c — dependent on Module__c
    if "SubModule__c" in have:
        print("  Case.SubModule__c: exists")
    elif dry:
        print("  Case.SubModule__c: WOULD create Picklist dependent on Module__c")
    else:
        all_subs = [s for subs in SUBMODULE_BY_MODULE.values() for s in subs]
        vsd = md.ValueSetValuesDefinition(
            sorted=False, value=[_cv(md, s) for s in all_subs])
        settings = [
            md.ValueSettings(valueName=s, controllingFieldValue=[mod])
            for mod, subs in SUBMODULE_BY_MODULE.items() for s in subs
        ]
        cf = md.CustomField(
            fullName="Case.SubModule__c", label="Sub-module", type="Picklist",
            valueSet=md.ValueSet(
                restricted=True, controllingField="Module__c",
                valueSetDefinition=vsd, valueSettings=settings))
        print(f"  Case.SubModule__c: {_create_ok(md.CustomField.create, cf)} (dependent on Module__c)")


def stage_cp_fields(sf, dry: bool) -> None:
    """Phase 27a — the case-control-plane Case fields (Routed_Team__c,
    Next_Action*, SLA_Breach__c, …). Idempotent: skips a field that exists."""
    md = sf.mdapi
    have = _case_fields(sf)
    for f in CONTROL_PLANE_FIELDS:
        short = f["api"].split(".")[1]
        if short in have:
            print(f"  {f['api']}: exists")
            continue
        if dry:
            spec = f["type"] + (f"({f.get('length') or f.get('precision','')})" if f.get("length") or f.get("precision") else "")
            print(f"  {f['api']}: WOULD create {spec}")
            continue
        kw: dict = {"fullName": f["api"], "label": f["label"], "type": f["type"], "required": False}
        if f["type"] == "Text":
            kw["length"] = f["length"]
        elif f["type"] == "Number":
            kw["precision"] = f["precision"]
            kw["scale"] = f["scale"]
        elif f["type"] == "Picklist":
            vsd = md.ValueSetValuesDefinition(sorted=False, value=[_cv(md, v) for v in f["values"]])
            kw["valueSet"] = md.ValueSet(restricted=True, valueSetDefinition=vsd)
        elif f["type"] == "Checkbox":
            kw["defaultValue"] = "false"
        note = _create_ok(md.CustomField.create, md.CustomField(**kw))
        print(f"  {f['api']}: {note} {f['type']}")


def stage_fls(sf, dry: bool) -> None:
    import os

    me = sf.query(
        f"SELECT ProfileId FROM User WHERE Username = '{os.environ['SF_USERNAME']}'"
    )["records"][0]["ProfileId"]
    ps_id = sf.query(
        "SELECT Id FROM PermissionSet WHERE IsOwnedByProfile = true "
        f"AND ProfileId = '{me}'"
    )["records"][0]["Id"]
    existing = {r["Field"] for r in sf.query(
        f"SELECT Field FROM FieldPermissions WHERE ParentId = '{ps_id}'")["records"]}
    for api in FLS_FIELDS:
        if api in existing:
            print(f"  {api}: FLS set")
            continue
        if dry:
            print(f"  {api}: WOULD grant FLS read/edit")
            continue
        try:
            sf.FieldPermissions.create({
                "ParentId": ps_id, "SobjectType": "Case", "Field": api,
                "PermissionsRead": True, "PermissionsEdit": True,
            })
            print(f"  {api}: FLS granted")
        except Exception as e:  # noqa: BLE001
            print(f"  {api}: FLS failed — {e}")


def stage_permset(sf, dry: bool) -> None:
    """A least-privilege Permission Set for the integration user — so the bot
    doesn't run as a full admin. Grants Case/EmailMessage/FeedItem/CaseComment
    + Contact/Account CRUD and read on the roster fields. Assign it, then
    downgrade the integration user's profile to 'Minimum Access - Salesforce'.
    """
    name = "Support_Bot_Integration"
    obj_perms = {
        "Case": dict(Read=True, Create=True, Edit=True, Delete=False, ViewAllRecords=True, ModifyAllRecords=False),
        "EmailMessage": dict(Read=True, Create=True, Edit=True, Delete=False, ViewAllRecords=True, ModifyAllRecords=False),
        "CaseComment": dict(Read=True, Create=True, Edit=True, Delete=False),
        "Contact": dict(Read=True, Create=True, Edit=True, Delete=False, ViewAllRecords=True, ModifyAllRecords=False),
        "Account": dict(Read=True, Create=True, Edit=True, Delete=False, ViewAllRecords=True, ModifyAllRecords=False),
    }
    rows = sf.query(f"SELECT Id FROM PermissionSet WHERE Name = '{name}'")["records"]
    if rows:
        ps_id = rows[0]["Id"]
        print(f"  PermissionSet {name}: exists ({ps_id})")
    elif dry:
        print(f"  PermissionSet {name}: WOULD create + grant {list(obj_perms)}")
        return
    else:
        ps_id = sf.PermissionSet.create({"Name": name, "Label": "Support Bot Integration"})["id"]
        print(f"  PermissionSet {name}: created ({ps_id})")
    if dry:
        return
    have = {r["SobjectType"] for r in sf.query(
        f"SELECT SobjectType FROM ObjectPermissions WHERE ParentId = '{ps_id}'")["records"]}
    for obj, perms in obj_perms.items():
        if obj in have:
            print(f"    {obj}: object perms set"); continue
        try:
            sf.ObjectPermissions.create({"ParentId": ps_id, "SobjectType": obj,
                                         **{f"Permissions{k}": v for k, v in perms.items()}})
            print(f"    {obj}: granted")
        except Exception as e:  # noqa: BLE001
            print(f"    {obj}: failed — {e}")
    print(f"  -> assign it:  System > Permission Sets > {name} > Manage Assignments > add the integration user")
    print("  -> then set that user's Profile to 'Minimum Access - Salesforce'")


STAGES = {"queues": stage_queues, "types": stage_types,
          "fields": stage_fields, "cp_fields": stage_cp_fields,
          "fls": stage_fls, "permset": stage_permset}


def _apply_tenant_taxonomy(tenant_id: str) -> None:
    """Pull Module/SubModule/Region/Case.Type picklist values from the same
    per-tenant taxonomy config `interpreter.case_taxonomy`'s
    map_case_fields/map_case_type use at runtime, instead of this script's
    own separately-hardcoded lists -- closes the two-sources-of-truth bug
    class migration 079 hit once (a rule produced a value the picklist
    didn't have). Only called when --tenant-id is passed; the no-flag
    default path is untouched, so existing usage is unaffected."""
    global CASE_TYPE_VALUES, MODULE_VALUES, REGION_VALUES, SUBMODULE_BY_MODULE, PICKLIST_CONVERT
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from interpreter.case_taxonomy import valid_values

    vv = valid_values(tenant_id)
    CASE_TYPE_VALUES = vv["case_types"]
    MODULE_VALUES = vv["modules"]
    REGION_VALUES = vv["regions"]
    SUBMODULE_BY_MODULE = vv["submodule_by_module"]
    PICKLIST_CONVERT = [
        {"api": "Case.Module__c", "label": "Module", "values": MODULE_VALUES},
        {"api": "Case.Region__c", "label": "Region", "values": REGION_VALUES},
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=list(STAGES), action="append",
                    help="run only these stage(s)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tenant-id", help="sync Module/SubModule/Region/Case.Type picklist "
                     "values from this tenant's case_taxonomy config (PUT "
                     "/api/tenants/case-taxonomy) instead of the built-in default")
    args = ap.parse_args()
    if not available():
        sys.exit("no SF creds in .env")
    if args.tenant_id:
        _apply_tenant_taxonomy(args.tenant_id)
    sf = _client()
    for name in (args.only or list(STAGES)):
        print(f"\n== {name} ==")
        STAGES[name](sf, args.dry_run)
    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
