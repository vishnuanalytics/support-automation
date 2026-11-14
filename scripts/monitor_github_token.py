"""Store a workspace's GitHub read token for Case Monitor, in Vault.

    python -m scripts.monitor_github_token --tenant <id> --repo owner/name

The token is typed at a hidden prompt, so it never lands in shell history. It
only needs read access to Issues on the repos your cases link to.
"""

from __future__ import annotations

import argparse
import getpass
import sys


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(".env")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--repo", required=True, help="a repo the token should read")
    args = parser.parse_args()

    import requests

    from ingestion.scraper import get_supabase
    from interpreter import audit, vault_secrets

    token = getpass.getpass("GitHub token: ").strip()
    r = requests.get(f"https://api.github.com/repos/{args.repo}/issues", timeout=15,
                     params={"per_page": 1},
                     headers={"Authorization": f"Bearer {token}",
                              "Accept": "application/vnd.github+json"})
    if r.status_code != 200:
        print(f"That token can't read {args.repo} issues (HTTP {r.status_code}). Nothing saved.")
        return 1
    sb = get_supabase()
    vault_id = vault_secrets.put(args.tenant, "github", {"token": token}, sb=sb)
    if not vault_id:
        print("Could not store the token. Nothing saved.")
        return 1
    sb.table("tenant_integrations").upsert({
        "tenant_id": args.tenant, "kind": "github", "vault_secret_id": vault_id,
        "secret": {"has_credentials": True}}).execute()
    audit.record(sb, tenant_id=args.tenant, action="github.connected",
                 target_type="integration", target_id="github",
                 summary="connected GitHub for Case Monitor")
    print(f"Saved. The token can read {args.repo}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
