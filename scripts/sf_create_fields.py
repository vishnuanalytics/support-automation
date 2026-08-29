"""
Create the custom fields the reference flow needs, plus field-level security
for the running user's profile — so you don't have to click through Setup.

    Case.Module__c    Text(120)   <- sf_writeback: classification.topic
    Case.Region__c    Text(80)    <- sf_writeback: region
    Account.Tier__c   Text(40)    <- classify tier_field (basic/premium/enterprise)

Uses the Metadata API (create the fields) + the REST API (grant FLS via a
FieldPermissions row on the profile's permission set). Idempotent: existing
fields / permissions are left alone.

Needs SF creds in .env (JWT or otherwise) and a user with "Customize
Application" (System Administrator has it).

    python scripts/sf_create_fields.py
"""

from __future__ import annotations

import pathlib
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from interpreter.salesforce import _client, available  # noqa: E402

FIELDS = [
    {"api": "Case.Module__c", "sobject": "Case", "label": "Module", "length": 120},
    {"api": "Case.Region__c", "sobject": "Case", "label": "Region", "length": 80},
    {"api": "Account.Tier__c", "sobject": "Account", "label": "Tier", "length": 40},
]


def _existing_fields(sf, sobject: str) -> set[str]:
    return {f["name"] for f in getattr(sf, sobject).describe()["fields"]}


def create_fields(sf) -> None:
    have = {s: _existing_fields(sf, s) for s in {f["sobject"] for f in FIELDS}}
    mdapi = sf.mdapi
    for f in FIELDS:
        short = f["api"].split(".")[1]
        if short in have[f["sobject"]]:
            print(f"  {f['api']}: already exists")
            continue
        cf = mdapi.CustomField(
            fullName=f["api"],
            label=f["label"],
            type="Text",
            length=f["length"],
            required=False,
        )
        mdapi.CustomField.create(cf)
        print(f"  {f['api']}: created (Text {f['length']})")


def grant_fls(sf) -> None:
    me = sf.restful("chatter/users/me")["id"]
    prof_id = sf.query(f"SELECT ProfileId FROM User WHERE Id = '{me}'")["records"][0]["ProfileId"]
    ps = sf.query(
        "SELECT Id, Name FROM PermissionSet "
        f"WHERE IsOwnedByProfile = true AND ProfileId = '{prof_id}'"
    )["records"][0]
    ps_id = ps["Id"]
    print(f"  profile permission set: {ps['Name']} ({ps_id})")

    existing = {
        r["Field"]
        for r in sf.query(
            f"SELECT Field FROM FieldPermissions WHERE ParentId = '{ps_id}'"
        )["records"]
    }
    for f in FIELDS:
        if f["api"] in existing:
            print(f"  {f['api']}: FLS already set")
            continue
        try:
            sf.FieldPermissions.create({
                "ParentId": ps_id,
                "SobjectType": f["sobject"],
                "Field": f["api"],
                "PermissionsRead": True,
                "PermissionsEdit": True,
            })
            print(f"  {f['api']}: FLS granted (read/edit)")
        except Exception as e:  # noqa: BLE001
            print(f"  {f['api']}: FLS create failed — {e}")


def main() -> int:
    if not available():
        sys.exit("no SF creds in .env — see SALESFORCE_SETUP.md")
    sf = _client()
    print("creating custom fields...")
    create_fields(sf)
    print("granting field-level security...")
    grant_fls(sf)
    print("\ndone. re-run the connection check to confirm they're writable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
