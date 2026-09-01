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
  * Screen Flow `Send_Bot_Draft_to_Customer` — confirm -> (optional edits) ->
                set Bot_Send_Draft__c = true -> "queued" screen.
                Runs in system context, so the field needs no profile FLS.
  * Quick Action `Case.Send_Bot_Draft` (type = Flow)

    python scripts/sf_deploy_send_draft_action.py            # deploy
    python scripts/sf_deploy_send_draft_action.py --remove   # delete all of it

Needs SF creds + "Author Apex" / "Modify Metadata" / "Manage Flow".

AFTER deploying: add the action to the Case page — Setup -> Object Manager ->
Case -> Page Layouts (or the Lightning record page in App Builder) -> drag
"Send Bot Draft to Customer" into the Salesforce Mobile & Lightning Actions.
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

FLOW = """\
<?xml version="1.0" encoding="UTF-8"?>
<Flow xmlns="http://soap.sforce.com/2006/04/metadata">
    <apiVersion>60.0</apiVersion>
    <environments>Default</environments>
    <interviewLabel>Send Bot Draft to Customer {!$Flow.CurrentDateTime}</interviewLabel>
    <label>Send Bot Draft to Customer</label>
    <processType>Flow</processType>
    <runInMode>SystemModeWithSharing</runInMode>
    <status>Active</status>
    <screens>
        <name>Confirm</name>
        <label>Confirm send</label>
        <allowBack>false</allowBack>
        <allowFinish>true</allowFinish>
        <allowPause>false</allowPause>
        <fields>
            <name>Confirm_info</name>
            <fieldText>&lt;p&gt;This queues the support bot&amp;#39;s suggested reply to be emailed to the customer. Add edits below if needed &amp;mdash; leave blank to send it as drafted.&lt;/p&gt;</fieldText>
            <fieldType>DisplayText</fieldType>
        </fields>
        <fields>
            <name>Edits</name>
            <fieldText>Edits (optional)</fieldText>
            <fieldType>LargeTextBox</fieldType>
            <isRequired>false</isRequired>
        </fields>
        <connector>
            <targetReference>Arm_send_flag</targetReference>
        </connector>
        <showFooter>true</showFooter>
        <showHeader>true</showHeader>
    </screens>
    <screens>
        <name>Queued</name>
        <label>Queued</label>
        <allowBack>false</allowBack>
        <allowFinish>true</allowFinish>
        <allowPause>false</allowPause>
        <fields>
            <name>Queued_info</name>
            <fieldText>&lt;p&gt;Queued. The customer will receive the reply within about a minute, and it will appear in this Case&amp;#39;s activity.&lt;/p&gt;</fieldText>
            <fieldType>DisplayText</fieldType>
        </fields>
        <showFooter>true</showFooter>
        <showHeader>true</showHeader>
    </screens>
    <recordUpdates>
        <name>Arm_send_flag</name>
        <label>Arm send flag</label>
        <connector>
            <targetReference>Queued</targetReference>
        </connector>
        <inputAssignments>
            <field>Bot_Send_Draft__c</field>
            <value>
                <booleanValue>true</booleanValue>
            </value>
        </inputAssignments>
        <inputAssignments>
            <field>Bot_Send_Note__c</field>
            <value>
                <elementReference>Edits</elementReference>
            </value>
        </inputAssignments>
        <filterLogic>and</filterLogic>
        <filters>
            <field>Id</field>
            <operator>EqualTo</operator>
            <value>
                <elementReference>recordId</elementReference>
            </value>
        </filters>
        <object>Case</object>
    </recordUpdates>
    <start>
        <connector>
            <targetReference>Confirm</targetReference>
        </connector>
    </start>
    <variables>
        <name>recordId</name>
        <dataType>String</dataType>
        <isCollection>false</isCollection>
        <isInput>true</isInput>
        <isOutput>false</isOutput>
    </variables>
</Flow>
"""

QUICK_ACTION = """\
<?xml version="1.0" encoding="UTF-8"?>
<QuickAction xmlns="http://soap.sforce.com/2006/04/metadata">
    <label>Send Bot Draft to Customer</label>
    <flowDefinition>Send_Bot_Draft_to_Customer</flowDefinition>
    <optionsCreateFeedItem>false</optionsCreateFeedItem>
    <type>Flow</type>
</QuickAction>
"""

PACKAGE = """\
<?xml version="1.0" encoding="UTF-8"?>
<Package xmlns="http://soap.sforce.com/2006/04/metadata">
    <types>
        <members>Case.Bot_Send_Draft__c</members>
        <members>Case.Bot_Send_Note__c</members>
        <name>CustomField</name>
    </types>
    <types><members>Send_Bot_Draft_to_Customer</members><name>Flow</name></types>
    <types><members>Case.Send_Bot_Draft</members><name>QuickAction</name></types>
    <version>%(api)s</version>
</Package>
""" % {"api": API}

_EMPTY_PACKAGE = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                  '<Package xmlns="http://soap.sforce.com/2006/04/metadata">'
                  f'<version>{API}</version></Package>\n')
# quick action first (it references the flow); flow next; fields last.
_DESTRUCTIVE = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<Package xmlns="http://soap.sforce.com/2006/04/metadata">\n'
                '  <types><members>Case.Send_Bot_Draft</members><name>QuickAction</name></types>\n'
                '  <types><members>Send_Bot_Draft_to_Customer</members><name>Flow</name></types>\n'
                '  <types><members>Case.Bot_Send_Draft__c</members>'
                '<members>Case.Bot_Send_Note__c</members><name>CustomField</name></types>\n'
                f'  <version>{API}</version>\n</Package>\n')


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("package.xml", PACKAGE)
        z.writestr("objects/Case.object", CASE_OBJECT)
        z.writestr("flows/Send_Bot_Draft_to_Customer.flow", FLOW)
        z.writestr("quickActions/Case.Send_Bot_Draft.quickAction", QUICK_ACTION)
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remove", action="store_true",
                    help="delete the action + flow + fields")
    args = ap.parse_args()
    if not available():
        sys.exit("no SF creds in .env")
    sf = _client()

    if args.remove:
        rc = _deploy(sf, _remove_zip(),
                     "removing Send_Bot_Draft action + flow + Case fields")
        print("\ndone." if rc == 0 else
              "\nremoval incomplete — an active Flow can't be deleted via API; "
              "deactivate 'Send Bot Draft to Customer' in Setup -> Flows, then re-run.")
        return rc

    rc = _deploy(sf, _zip(), "deploying Send_Bot_Draft action + flow + Case fields")
    if rc == 0:
        print("\ndone. Now add the button to the Case page:")
        print("  Setup -> Object Manager -> Case -> Page Layouts -> (your layout)")
        print("  -> Mobile & Lightning Actions -> drag 'Send Bot Draft to Customer'")
        print("  into the Salesforce Mobile and Lightning Experience Actions row.")
        print("\nThen: open an escalated Case, click the button, confirm. The worker")
        print("emails the bot's draft and clears Bot_Send_Draft__c within ~1 min.")
    else:
        print("\ndeploy failed — see the component failures above. If the Flow")
        print("failed to activate, change <status>Active</status> to Draft in this")
        print("script, re-deploy, then activate it manually in Setup -> Flows.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
