"""
Deploy the Salesforce -> automation **push** trigger (Phase 20i):

  * Named Credential `SupportAutomation`  -> the automation's public URL
  * Apex class    `SupportAutomationHook`  -> @future POST to
                  callout:SupportAutomation/api/hooks/salesforce/case
  * Apex trigger  `SupportAutomationCaseTrigger` on Case (after insert)
                  -> fires for a new Status='New', non-Email Case

Swapping the endpoint later (webhook.site -> Oracle Cloud -> …) = edit the
Named Credential URL in Setup; no redeploy.

    python scripts/sf_deploy_case_hook.py https://webhook.site/<uuid>
    python scripts/sf_deploy_case_hook.py --url https://my-api.example.com
    python scripts/sf_deploy_case_hook.py --remove   # delete the trigger + class + cred

Needs SF creds + "Author Apex" / "Modify Metadata". Works on Developer
Edition without test coverage.

REMOVE this when the CDC / Pub-Sub subscriber (Phase 20l) is your
Salesforce -> app push path: that one is outbound-only and needs no public
URL, so the Apex @future callout here is redundant and — with the API
unreachable — throws `CalloutException: Unable to tunnel through proxy` and
emails the org admin on every Case.
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import pathlib
import sys
import time
import zipfile

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from interpreter.salesforce import _client, available  # noqa: E402

API = "60.0"

APEX_CLASS = """\
public with sharing class SupportAutomationHook {
    // Shared secret — must match SF_HOOK_SECRET in the automation's environment.
    private static final String SECRET = '%(secret)s';

    @future(callout=true)
    public static void notify(Set<Id> caseIds) {
        String err = null;
        for (Id cid : caseIds) {
            HttpRequest req = new HttpRequest();
            req.setEndpoint('callout:SupportAutomation/api/hooks/salesforce/case');
            req.setMethod('POST');
            req.setHeader('Content-Type', 'application/json');
            req.setHeader('X-SF-Hook-Secret', SECRET);
            req.setBody('{"case_id":"' + String.valueOf(cid) + '"}');
            req.setTimeout(30000);
            try {
                HttpResponse res = new Http().send(req);
                System.debug(LoggingLevel.INFO, 'SupportAutomationHook ' + cid
                    + ' -> ' + res.getStatusCode() + ' ' + res.getBody());
                if (res.getStatusCode() >= 300) {
                    err = cid + ': HTTP ' + res.getStatusCode() + ' ' + res.getBody();
                }
            } catch (Exception e) {
                err = cid + ': ' + e.getTypeName() + ' ' + e.getMessage();
                System.debug(LoggingLevel.ERROR, 'SupportAutomationHook ' + err);
            }
        }
        // surface a failure on the AsyncApexJob (Status=Failed / ExtendedStatus)
        if (err != null) { throw new CalloutException(err); }
    }
}
"""

APEX_TRIGGER = """\
trigger SupportAutomationCaseTrigger on Case (after insert) {
    Set<Id> toNotify = new Set<Id>();
    for (Case c : Trigger.new) {
        // Email-origin Cases are handled by the email channel; everything
        // else (Web, Phone, manual, web-to-case) goes through the router.
        if (c.Status == 'New' && c.Origin != 'Email') {
            toNotify.add(c.Id);
        }
    }
    if (!toNotify.isEmpty()) {
        SupportAutomationHook.notify(toNotify);
    }
}
"""

NAMED_CRED = """\
<?xml version="1.0" encoding="UTF-8"?>
<NamedCredential xmlns="http://soap.sforce.com/2006/04/metadata">
    <label>Support Automation</label>
    <endpoint>%(url)s</endpoint>
    <principalType>Anonymous</principalType>
    <protocol>NoAuthentication</protocol>
    <generateAuthorizationHeader>false</generateAuthorizationHeader>
    <allowMergeFieldsInBody>false</allowMergeFieldsInBody>
    <allowMergeFieldsInHeader>false</allowMergeFieldsInHeader>
</NamedCredential>
"""

PACKAGE = """\
<?xml version="1.0" encoding="UTF-8"?>
<Package xmlns="http://soap.sforce.com/2006/04/metadata">
    <types><members>SupportAutomationHook</members><name>ApexClass</name></types>
    <types><members>SupportAutomationCaseTrigger</members><name>ApexTrigger</name></types>
    <types><members>SupportAutomation</members><name>NamedCredential</name></types>
    <version>%(api)s</version>
</Package>
""" % {"api": API}


_EMPTY_PACKAGE = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                  '<Package xmlns="http://soap.sforce.com/2006/04/metadata">'
                  f'<version>{API}</version></Package>\n')
# trigger before class (it references the class); cred last.
_DESTRUCTIVE = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<Package xmlns="http://soap.sforce.com/2006/04/metadata">\n'
                '  <types><members>SupportAutomationCaseTrigger</members>'
                '<name>ApexTrigger</name></types>\n'
                '  <types><members>SupportAutomationHook</members>'
                '<name>ApexClass</name></types>\n'
                '  <types><members>SupportAutomation</members>'
                '<name>NamedCredential</name></types>\n'
                f'  <version>{API}</version>\n</Package>\n')


def _remove_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("package.xml", _EMPTY_PACKAGE)
        z.writestr("destructiveChanges.xml", _DESTRUCTIVE)
    return buf.getvalue()


def _zip(secret: str, url: str) -> bytes:
    meta = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<ApexClass xmlns="http://soap.sforce.com/2006/04/metadata">'
            f'<apiVersion>{API}</apiVersion><status>Active</status></ApexClass>\n')
    tmeta = meta.replace("ApexClass", "ApexTrigger")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("package.xml", PACKAGE)
        z.writestr("classes/SupportAutomationHook.cls", APEX_CLASS % {"secret": secret})
        z.writestr("classes/SupportAutomationHook.cls-meta.xml", meta)
        z.writestr("triggers/SupportAutomationCaseTrigger.trigger", APEX_TRIGGER)
        z.writestr("triggers/SupportAutomationCaseTrigger.trigger-meta.xml", tmeta)
        z.writestr("namedCredentials/SupportAutomation.namedCredential", NAMED_CRED % {"url": url})
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", help="the automation's public base URL")
    ap.add_argument("--url", dest="url_opt")
    ap.add_argument("--remove", action="store_true",
                    help="delete the trigger + class + Named Credential")
    args = ap.parse_args()
    if not available():
        sys.exit("no SF creds in .env")

    sf = _client()
    import tempfile
    zpath = pathlib.Path(tempfile.gettempdir()) / "sf_case_hook.zip"

    if args.remove:
        zpath.write_bytes(_remove_zip())
        print("removing SupportAutomationCaseTrigger + SupportAutomationHook + "
              "SupportAutomation Named Credential …")
        dep = sf.deploy(str(zpath), sandbox=False, testLevel="NoTestRun")
        aid = dep["asyncId"] if isinstance(dep, dict) else dep[0]
        res: dict = {}
        for _ in range(40):
            time.sleep(3)
            res = sf.checkDeployStatus(aid, include_details=True)
            if (res or {}).get("state") not in (None, "", "InProgress", "Pending", "Queued"):
                break
        print(f"deploy: state={res.get('state')} success={res.get('success')}")
        for f in (res.get("deployment_detail", {}) or {}).get("componentFailures", []) or []:
            print("  FAIL", f.get("fullName"), "-", f.get("problem"))
        ok = res.get("state") in ("Succeeded", "Completed")
        print("\ndone — the Apex push path is gone; CDC / Pub-Sub is now the only "
              "Salesforce -> app push." if ok else "\nremoval did not fully succeed — "
              "delete the trigger/class/cred in Setup manually.")
        return 0 if ok else 1

    url = (args.url or args.url_opt or "").rstrip("/")
    if not url:
        sys.exit("pass the public base URL (e.g. https://webhook.site/<uuid>), or --remove")
    secret = os.environ.get("SF_HOOK_SECRET")
    if not secret:
        sys.exit("SF_HOOK_SECRET not in .env")

    zpath.write_bytes(_zip(secret, url))
    print(f"deploying push trigger -> {url}")
    dep = sf.deploy(str(zpath), sandbox=False, testLevel="NoTestRun")
    aid = dep["asyncId"] if isinstance(dep, dict) else dep[0]
    print(f"deploy id {aid} — polling…")
    res: dict = {}
    for _ in range(40):
        time.sleep(3)
        res = sf.checkDeployStatus(aid, include_details=True)
        state = (res or {}).get("state")
        if state not in (None, "", "InProgress", "Pending", "Queued"):
            break
    print(f"deploy: state={res.get('state')} success={res.get('success')}")
    if res.get("state") not in ("Succeeded", "Completed"):
        for f in (res.get("deployment_detail", {}) or {}).get("componentFailures", []) or []:
            print("  FAIL", f.get("fullName"), "-", f.get("problem"))
        print(res)
        return 1
    print("\ndone. A new Case (Status=New, Origin != Email) now POSTs "
          f"{url}/api/hooks/salesforce/case with the X-SF-Hook-Secret header.")
    print("Swap the endpoint later: Setup -> Named Credentials -> Support "
          "Automation -> edit URL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
