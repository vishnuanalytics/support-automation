"""Connect a workspace to Salesforce using your Salesforce CLI login.

For orgs where you can't get a Connected App client ID and secret. Log in once,
then hand that OAuth login to the workspace so the worker can sync without you:

    sf org login web --alias acme
    python -m scripts.sf_connect_cli --alias acme            # list workspaces
    python -m scripts.sf_connect_cli --alias acme --tenant <workspace id>

The refresh token goes into Vault and is never printed. It has your Salesforce
permissions. Run `sf org logout --target-org acme` to revoke it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys


def read_cli_login(alias: str) -> dict[str, str]:
    from interpreter import salesforce_oauth

    out = subprocess.run(
        ["sf", "org", "auth", "show-sfdx-auth-url", "--json", "--target-org", alias],
        capture_output=True, text=True, check=False)
    try:
        body = json.loads(out.stdout)
    except ValueError:
        raise SystemExit("Could not read the sf CLI output. Is the sf CLI installed?") from None
    url = (body.get("result") or {}).get("sfdxAuthUrl")
    if not url:
        raise SystemExit(f"No login found for {alias!r}. Run: sf org login web --alias {alias}")
    try:
        login = salesforce_oauth.parse_sfdx_auth_url(url)
    except ValueError:
        raise SystemExit("The sf CLI returned an auth URL in an unexpected format.") from None
    if login["client_secret"]:
        raise SystemExit("This login uses a client secret, so it isn't a public CLI login.")
    return login


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(".env")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--alias", required=True)
    parser.add_argument("--tenant", help="workspace id; omit to list workspaces")
    parser.add_argument("--org", default="default", help="org label in the workspace")
    args = parser.parse_args()

    from ingestion.scraper import get_supabase
    from interpreter import audit, salesforce, salesforce_oauth

    sb = get_supabase()
    if not args.tenant:
        for row in sb.table("tenants").select("tenant_id, name").execute().data or []:
            print(row["tenant_id"], row.get("name") or "")
        return 0

    login = read_cli_login(args.alias)
    client = salesforce_oauth.client_from_oauth(
        login["refresh_token"], login["instance_url"], login["client_id"])
    # Not every user can read Organization, and Case is what a sync needs.
    cases = client.query("SELECT COUNT() FROM Case")["totalSize"]
    salesforce.save_tenant_org(args.tenant, args.org, {
        "SF_OAUTH_REFRESH_TOKEN": login["refresh_token"],
        "SF_OAUTH_INSTANCE_URL": login["instance_url"],
        "SF_OAUTH_PUBLIC_CLIENT_ID": login["client_id"],
    }, sb=sb)
    audit.record(sb, tenant_id=args.tenant, action="salesforce_org.connected",
                 target_type="salesforce_org", target_id=args.org,
                 summary=f"connected Salesforce org {args.org!r} via CLI login")
    print(f"Connected {login['instance_url']} as {args.org!r} ({cases} cases readable).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
