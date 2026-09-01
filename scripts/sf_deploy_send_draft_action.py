"""
Deploy the Salesforce **"Send Bot Draft to Customer"** Quick Action (Phase 23h).

The button can't call our API directly — this org's outbound callouts tunnel
through a proxy that 503s (that's why the Phase 20i Apex hook was retired).
So the button just **arms a Case field**; the always-on CDC / Pub-Sub
subscriber sees the `CaseChangeEvent`, and `api.worker` sends the bot's
drafted reply and clears the field again.

Deploys (Metadata API, no Apex, no test coverage needed):

  * Case field  `Bot_Send_Draft__c`  (Checkbox, default false)  — the arm flag
  * Case field  `Bot_Send_Note__c`   (LongTextArea 4096)        — optional edits
  * Quick Action `Case.Send_Bot_Draft` — type **Update**: shows the arm
    checkbox + the note; Save fires the CDC event. (A Flow-type action can't
    be added to a page-layout action list, so Update it is.)
  * FLS for both fields on the **Admin** profile — a Metadata-API field is
    invisible to every profile, System Administrator included, until granted.

    python scripts/sf_deploy_send_draft_action.py            # deploy
    python scripts/sf_deploy_send_draft_action.py --remove   # delete all of it

Needs SF creds + "Modify Metadata" / "Customize Application".

AFTER deploying, add the action to the Case layouts' action lists — done for
this org by appending `Case.Send_Bot_Draft` to each `Layout.quickActionList`
via `sf.mdapi` (see the session notes); in the UI it's Setup -> Object
Manager -> Case -> Page Layouts -> Mobile & Lightning Actions.
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

API = "60.0"

CASE_OBJECT = """\
<?xml version="1.0" encoding="UTF-8"?>
<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">
    <fields>
        <fullName>Bot_Send_Draft__c</fullName>
        <externalId>false</externalId>
        <label>Bot: Send Draft</label>
        <type>Checkbox</type>
        <defaultValue>false</defaultValue>
        <description>Set by the "Send Bot Draft to Customer" action. The support
            automation worker sees the change, emails the bot's drafted reply to
            the customer, and clears this box.</description>
    </fields>
    <fields>
        <fullName>Bot_Send_Note__c</fullName>
        <externalId>false</externalId>
        <label>Bot: Send Note</label>
        <type>LongTextArea</type>
        <length>4096</length>
        <visibleLines>4</visibleLines>
        <required>false</required>
        <description>Optional edits an agent typed on the confirm screen. Blank
            means "send the draft as-is". Cleared after the reply is sent.</description>
    </fields>
</CustomObject>
"""

# An **Update** action (not Flow): a Flow quick action can't be added to a
# page layout's action list, an Update one can. It shows the arm checkbox +
# an optional note; Save fires the CDC event the worker listens for.
QUICK_ACTION = """\
<?xml version="1.0" encoding="UTF-8"?>
<QuickAction xmlns="http://soap.sforce.com/2006/04/metadata">
    <label>Send Bot Draft to Customer</label>
    <optionsCreateFeedItem>false</optionsCreateFeedItem>
    <quickActionLayout>
        <layoutSectionStyle>TwoColumnsLeftToRight</layoutSectionStyle>
        <quickActionLayoutColumns>
            <quickActionLayoutItems>
                <emptySpace>false</emptySpace>
                <field>Bot_Send_Draft__c</field>
                <uiBehavior>Edit</uiBehavior>
            </quickActionLayoutItems>
            <quickActionLayoutItems>
                <emptySpace>false</emptySpace>
                <field>Bot_Send_Note__c</field>
                <uiBehavior>Edit</uiBehavior>
            </quickActionLayoutItems>
        </quickActionLayoutColumns>
        <quickActionLayoutColumns>
            <quickActionLayoutItems>
                <emptySpace>true</emptySpace>
            </quickActionLayoutItems>
        </quickActionLayoutColumns>
    </quickActionLayout>
    <targetObject>Case</targetObject>
    <type>Update</type>
</QuickAction>
"""

# The integration user (worker) reads Bot_Send_Note__c and clears
# Bot_Send_Draft__c. A field created via the Metadata API has no field-level
# security for anyone — not even System Administrator — so grant it here.
# A Profile deploy is additive: only these fieldPermissions are touched.
ADMIN_PROFILE = """\
<?xml version="1.0" encoding="UTF-8"?>
<Profile xmlns="http://soap.sforce.com/2006/04/metadata">
    <fieldPermissions>
        <field>Case.Bot_Send_Draft__c</field>
        <editable>true</editable>
        <readable>true</readable>
    </fieldPermissions>
    <fieldPermissions>
        <field>Case.Bot_Send_Note__c</field>
        <editable>true</editable>
        <readable>true</readable>
    </fieldPermissions>
</Profile>
"""

PACKAGE = """\
<?xml version="1.0" encoding="UTF-8"?>
<Package xmlns="http://soap.sforce.com/2006/04/metadata">
    <types>
        <members>Case.Bot_Send_Draft__c</members>
        <members>Case.Bot_Send_Note__c</members>
        <name>CustomField</name>
    </types>
    <types><members>Case.Send_Bot_Draft</members><name>QuickAction</name></types>
    <types><members>Admin</members><name>Profile</name></types>
    <version>%(api)s</version>
</Package>
""" % {"api": API}

_EMPTY_PACKAGE = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                  '<Package xmlns="http://soap.sforce.com/2006/04/metadata">'
                  f'<version>{API}</version></Package>\n')
# quick action before the fields it references.
_DESTRUCTIVE = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<Package xmlns="http://soap.sforce.com/2006/04/metadata">\n'
                '  <types><members>Case.Send_Bot_Draft</members><name>QuickAction</name></types>\n'
                '  <types><members>Case.Bot_Send_Draft__c</members>'
                '<members>Case.Bot_Send_Note__c</members><name>CustomField</name></types>\n'
                f'  <version>{API}</version>\n</Package>\n')


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("package.xml", PACKAGE)
        z.writestr("objects/Case.object", CASE_OBJECT)
        z.writestr("quickActions/Case.Send_Bot_Draft.quickAction", QUICK_ACTION)
        z.writestr("profiles/Admin.profile", ADMIN_PROFILE)
    return buf.getvalue()


def _remove_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("package.xml", _EMPTY_PACKAGE)
        z.writestr("destructiveChanges.xml", _DESTRUCTIVE)
    return buf.getvalue()


def _deploy(sf, data: bytes, what: str) -> int:
    zpath = pathlib.Path(tempfile.gettempdir()) / "sf_send_draft_action.zip"
    zpath.write_bytes(data)
    print(f"{what} …")
    dep = sf.deploy(str(zpath), sandbox=False, testLevel="NoTestRun")
    aid = dep["asyncId"] if isinstance(dep, dict) else dep[0]
    print(f"deploy id {aid} — polling…")
    res: dict = {}
    for _ in range(60):
        time.sleep(3)
        res = sf.checkDeployStatus(aid, include_details=True)
        if (res or {}).get("state") not in (None, "", "InProgress", "Pending", "Queued"):
            break
    print(f"deploy: state={res.get('state')} success={res.get('success')}")
    for f in (res.get("deployment_detail", {}) or {}).get("componentFailures", []) or []:
        print("  FAIL", f.get("fullName"), "-", f.get("problem"))
    return 0 if res.get("state") in ("Succeeded", "Completed") else 1


def _add_to_layouts(sf) -> None:
    """Append `Case.Send_Bot_Draft` to every Case layout's action list so the
    button shows in Lightning. Idempotent."""
    action = "Case.Send_Bot_Draft"
    for full in ("Case-Case Layout", "Case-Case (Marketing) Layout",
                 "Case-Case (Sales) Layout", "Case-Case (Support) Layout"):
        try:
            lay = sf.mdapi.Layout.read(full)
        except Exception as e:  # noqa: BLE001
            print(f"  [skip] {full}: {repr(e)[:100]}")
            continue
        qal = lay.quickActionList
        if qal is None:
            lay.quickActionList = {"quickActionListItems": [{"quickActionName": action}]}
        elif action in [i["quickActionName"] for i in qal["quickActionListItems"]]:
            print(f"  [ok]   {full}: already has it")
            continue
        else:
            qal["quickActionListItems"].append({"quickActionName": action})
        try:
            sf.mdapi.Layout.update(lay)
            print(f"  [done] {full}")
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] {full}: {repr(e)[:150]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remove", action="store_true",
                    help="delete the action + Case fields")
    ap.add_argument("--skip-layouts", action="store_true",
                    help="don't touch the Case page layouts")
    args = ap.parse_args()
    if not available():
        sys.exit("no SF creds in .env")
    sf = _client()

    if args.remove:
        rc = _deploy(sf, _remove_zip(),
                     "removing Send_Bot_Draft action + Case fields")
        print("\ndone (remove the action from the Case layouts by hand if needed)."
              if rc == 0 else "\nremoval incomplete — see the failures above.")
        return rc

    rc = _deploy(sf, _zip(), "deploying Send_Bot_Draft action + Case fields + FLS")
    if rc != 0:
        print("\ndeploy failed — see the component failures above.")
        return rc
    if not args.skip_layouts:
        print("\nadding the action to the Case layouts:")
        _add_to_layouts(sf)
    print("\ndone. Open an escalated Case -> 'Send Bot Draft to Customer' -> tick")
    print("the box -> Save. The worker emails the bot's draft and clears the")
    print("box within ~1 min.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
