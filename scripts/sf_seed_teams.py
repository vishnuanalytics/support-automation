"""
Seed the support-team roster in Salesforce for the Case-router workflow
(Phase 20i). Dev Edition caps Salesforce-licensed Users at 4 (2 free), so:

  * 2 real Users  — the Support and CSM team managers, added to their queues
  * 13 Contacts   — everyone else, on an "Internal — Support Teams" Account,
                    tagged Team__c + TeamRole__c

Structure (confirmed with the project owner): each team = 1 Manager + 2
Members. Teams: Support, CSM, Sales, Offboarding, Email.

    python scripts/sf_seed_teams.py            # idempotent
    python scripts/sf_seed_teams.py --dry-run

Needs SF creds in .env + "Customize Application" / "Modify Metadata".
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from interpreter.salesforce import _client, available  # noqa: E402

TEAM_VALUES = ["Support", "CSM", "Sales", "Offboarding", "Email"]
ROLE_VALUES = ["Manager", "Member"]

# 2 real Users (managers). username is a unique fake; email is where SF would
# send notifications — kept on a domain we don't own so nothing escapes.
USERS = [
    {"key": "sam", "FirstName": "Sam", "LastName": "Rivera", "team": "Support",
     "Username": "sam.rivera@caserouter.example.com", "Email": "sam.rivera@caserouter.example.com",
     "Alias": "srivera", "profile": "Custom: Support Profile", "queue": "Team_Support"},
    {"key": "casey", "FirstName": "Casey", "LastName": "Lin", "team": "CSM",
     "Username": "casey.lin@caserouter.example.com", "Email": "casey.lin@caserouter.example.com",
     "Alias": "clin", "profile": "Standard User", "queue": "Team_CSM"},
]

# 13 Contacts. Support/CSM already have a User manager -> 2 Members each.
# Sales/Offboarding/Email -> 1 Manager + 2 Members each.
CONTACTS = (
    [{"FirstName": "Dana", "LastName": "Okafor", "team": "Support", "role": "Member"},
     {"FirstName": "Ravi", "LastName": "Menon", "team": "Support", "role": "Member"},
     {"FirstName": "Priya", "LastName": "Shah", "team": "CSM", "role": "Member"},
     {"FirstName": "Tom", "LastName": "Becker", "team": "CSM", "role": "Member"}]
    + [{"FirstName": f, "LastName": l, "team": t, "role": r}
       for t, (mgr, m1, m2) in {
           "Sales": (("Alex", "Ng"), ("Jordan", "Cole"), ("Sofia", "Ramos")),
           "Offboarding": (("Lena", "Park"), ("Marcus", "Hale"), ("Ivy", "Doyle")),
           "Email": (("Nora", "Blum"), ("Owen", "Frey"), ("Zoe", "Kaur")),
       }.items()
       for (f, l), r in zip((mgr, m1, m2), ROLE_VALUES[:1] + ["Member", "Member"])]
)

ROSTER_ACCOUNT = "Internal — Support Teams"


def _ensure_contact_fields(sf, dry: bool) -> None:
    md = sf.mdapi
    have = {f["name"] for f in sf.Contact.describe()["fields"]}
    for api, label, vals in [("Contact.Team__c", "Team", TEAM_VALUES),
                             ("Contact.TeamRole__c", "Team Role", ROLE_VALUES)]:
        short = api.split(".")[1]
        if short in have:
            print(f"  {api}: exists"); continue
        if dry:
            print(f"  {api}: WOULD create Picklist {vals}"); continue
        vsd = md.ValueSetValuesDefinition(
            sorted=False,
            value=[md.CustomValue(fullName=v, label=v, default=False) for v in vals])
        md.CustomField.create(md.CustomField(
            fullName=api, label=label, type="Picklist",
            valueSet=md.ValueSet(restricted=True, valueSetDefinition=vsd)))
        print(f"  {api}: created Picklist {vals}")
    # FLS on the admin profile's permission set
    if not dry:
        me = sf.query(
            "SELECT ProfileId FROM User WHERE Username = '%s'"
            % __import__("os").environ["SF_USERNAME"])["records"][0]["ProfileId"]
        ps = sf.query("SELECT Id FROM PermissionSet WHERE IsOwnedByProfile = true "
                      f"AND ProfileId = '{me}'")["records"][0]["Id"]
        existing = {r["Field"] for r in sf.query(
            f"SELECT Field FROM FieldPermissions WHERE ParentId = '{ps}'")["records"]}
        for api in ("Contact.Team__c", "Contact.TeamRole__c"):
            if api in existing:
                continue
            try:
                sf.FieldPermissions.create({
                    "ParentId": ps, "SobjectType": "Contact", "Field": api,
                    "PermissionsRead": True, "PermissionsEdit": True})
                print(f"  {api}: FLS granted")
            except Exception as e:  # noqa: BLE001
                print(f"  {api}: FLS failed — {e}")


def _admin_defaults(sf) -> dict:
    a = sf.query(
        "SELECT TimeZoneSidKey, LocaleSidKey, EmailEncodingKey, LanguageLocaleKey "
        "FROM User WHERE Username = '%s'" % __import__("os").environ["SF_USERNAME"]
    )["records"][0]
    return {k: a[k] for k in a if not k.startswith("attributes")}


def _profile_ids(sf) -> dict:
    return {r["Name"]: r["Id"] for r in
            sf.query("SELECT Id, Name FROM Profile")["records"]}


def _queue_ids(sf) -> dict:
    return {r["DeveloperName"]: r["Id"] for r in
            sf.query("SELECT Id, DeveloperName FROM Group WHERE Type = 'Queue'")["records"]}


def seed_users(sf, dry: bool) -> dict:
    profs = _profile_ids(sf)
    queues = _queue_ids(sf)
    base = _admin_defaults(sf)
    out: dict[str, str] = {}
    for u in USERS:
        rows = sf.query(f"SELECT Id FROM User WHERE Username = '{u['Username']}'")["records"]
        if rows:
            out[u["key"]] = rows[0]["Id"]
            print(f"  user {u['Username']}: exists")
        elif dry:
            print(f"  user {u['Username']}: WOULD create ({u['profile']}, mgr of {u['team']})")
            continue
        else:
            res = sf.User.create({
                "FirstName": u["FirstName"], "LastName": u["LastName"],
                "Username": u["Username"], "Email": u["Email"], "Alias": u["Alias"],
                "ProfileId": profs[u["profile"]], "IsActive": True, **base,
            })
            out[u["key"]] = res["id"]
            print(f"  user {u['Username']}: created {res['id']}")
        # queue membership
        uid = out.get(u["key"])
        qid = queues.get(u["queue"])
        if uid and qid and not dry:
            dup = sf.query("SELECT Id FROM GroupMember WHERE GroupId = "
                           f"'{qid}' AND UserOrGroupId = '{uid}'")["records"]
            if not dup:
                sf.GroupMember.create({"GroupId": qid, "UserOrGroupId": uid})
                print(f"    added to queue {u['queue']}")
    return out


def seed_contacts(sf, users: dict, dry: bool) -> None:
    acc = sf.query(f"SELECT Id FROM Account WHERE Name = '{ROSTER_ACCOUNT}'")["records"]
    if acc:
        acc_id = acc[0]["Id"]
    elif dry:
        acc_id = "(new)"
    else:
        acc_id = sf.Account.create({"Name": ROSTER_ACCOUNT,
                                    "Description": "Support team roster (Phase 20i)."})["id"]
        print(f"  account {ROSTER_ACCOUNT!r}: created {acc_id}")

    have = {(r["FirstName"], r["LastName"]) for r in sf.query(
        "SELECT FirstName, LastName FROM Contact WHERE Account.Name = "
        f"'{ROSTER_ACCOUNT}'")["records"]}
    for c in CONTACTS:
        if (c["FirstName"], c["LastName"]) in have:
            print(f"  contact {c['FirstName']} {c['LastName']}: exists"); continue
        if dry:
            print(f"  contact {c['FirstName']} {c['LastName']}: WOULD create "
                  f"({c['team']} / {c['role']})")
            continue
        sf.Contact.create({
            "FirstName": c["FirstName"], "LastName": c["LastName"],
            "Email": f"{c['FirstName'].lower()}.{c['LastName'].lower()}@caserouter.example.com",
            "AccountId": acc_id, "Team__c": c["team"], "TeamRole__c": c["role"],
        })
        print(f"  contact {c['FirstName']} {c['LastName']}: created ({c['team']} / {c['role']})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not available():
        sys.exit("no SF creds in .env")
    sf = _client()
    print("== Contact fields ==");  _ensure_contact_fields(sf, args.dry_run)
    print("== Users (managers) ==")
    users = seed_users(sf, args.dry_run)
    print("== Contacts (roster) ==")
    seed_contacts(sf, users, args.dry_run)
    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
