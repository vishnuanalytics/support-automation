"""
Set a password on a Supabase Auth user so you can sign in with
email + password instead of a magic link.

    python -m scripts.set_editor_password EMAIL [PASSWORD]

If PASSWORD is omitted a strong one is generated and printed. Uses
SUPABASE_URL / SUPABASE_SERVICE_KEY / SUPABASE_ANON_KEY from .env.
"""

from __future__ import annotations

import pathlib
import secrets
import sys

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

import os

URL = os.environ["SUPABASE_URL"]
SERVICE = os.environ["SUPABASE_SERVICE_KEY"]
ANON = os.environ.get("SUPABASE_ANON_KEY", SERVICE)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    email = sys.argv[1].strip().lower()
    password = sys.argv[2] if len(sys.argv) > 2 else f"Sf-{secrets.token_urlsafe(9)}"

    h = {"apikey": SERVICE, "Authorization": f"Bearer {SERVICE}",
         "Content-Type": "application/json"}

    r = requests.get(f"{URL}/auth/v1/admin/users", headers=h,
                     params={"page": 1, "per_page": 500}, timeout=20)
    r.raise_for_status()
    users = r.json().get("users", [])
    me = next((u for u in users if (u.get("email") or "").lower() == email), None)
    if not me:
        print(f"no auth user for {email!r}. present: "
              f"{[u.get('email') for u in users]}")
        return 1

    up = requests.put(
        f"{URL}/auth/v1/admin/users/{me['id']}", headers=h,
        json={"password": password, "email_confirm": True}, timeout=20,
    )
    up.raise_for_status()

    lg = requests.post(
        f"{URL}/auth/v1/token?grant_type=password",
        headers={"apikey": ANON, "Content-Type": "application/json"},
        json={"email": email, "password": password}, timeout=20,
    )
    ok = lg.status_code == 200
    print(f"user   : {email}  ({me['id']})")
    print(f"password: {password}")
    print(f"login  : {'OK — sign in with email + password' if ok else 'FAILED: ' + lg.text[:200]}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
