"""
Push the local .env (+ the inline Salesforce JWT key) to a Hugging Face
Space as Secrets, then restart the Space so it rebuilds with them.

    HF_TOKEN=hf_xxx python deploy/set_hfspace_secrets.py vishnureddy7/support_automation

Run from the repo root. Reads ./.env and ./sf_jwt/server.key. The token
is read from $HF_TOKEN and never written anywhere.
"""

from __future__ import annotations

import os
import pathlib
import sys

from huggingface_hub import HfApi, add_space_secret

# vars the daemons don't use / that must not be copied verbatim
SKIP = {
    "SF_PRIVATE_KEY_FILE",              # replaced by inline SF_PRIVATE_KEY
    "AURA_INSTANCEID", "AURA_INSTANCENAME",
    "WEB_ORIGINS",                      # API-only
}


def parse_env(p: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit("usage: HF_TOKEN=hf_xxx python deploy/set_hfspace_secrets.py <user>/<space>")
    repo = sys.argv[1]
    token = os.environ.get("HF_TOKEN") or sys.exit("set HF_TOKEN (a write token)")

    root = pathlib.Path.cwd()
    env = parse_env(root / ".env")
    key_file = root / "sf_jwt" / "server.key"
    if key_file.exists():
        env["SF_PRIVATE_KEY"] = key_file.read_text()

    n = 0
    for k, v in env.items():
        if k in SKIP or not v:
            print(f"  skip {k}")
            continue
        add_space_secret(repo_id=repo, key=k, value=v, token=token)
        n += 1
        print(f"  set  {k} = {v[:4] + '…' if len(v) > 8 else v}")

    print(f"\n{n} secrets set; restarting {repo} …")
    HfApi(token=token).restart_space(repo_id=repo)
    print("done — watch the Space 'Logs' tab for the rebuild.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
