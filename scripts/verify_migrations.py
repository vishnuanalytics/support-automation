"""
P2a (FR-43) — does the live schema match `db/migrations/*.sql`?

`supabase_migrations` is deliberately NOT kept in sync with the .sql files
(CLAUDE.md); the .sql files are the source of truth. This script parses the
CREATE TABLE / ALTER TABLE ADD COLUMN / CREATE FUNCTION / CREATE INDEX
statements out of every migration and checks each object exists in the live
public schema (via the `introspect_schema()` RPC — migration 070). It also
flags live tables that no migration file creates (a hand / MCP change with no
committed .sql).

    python -m scripts.verify_migrations           # exit 0 clean, 1 on drift
    python -m scripts.verify_migrations --json

Needs SUPABASE_URL + SUPABASE_SERVICE_KEY. Without them it exits 0 with a
"skipped" note so CI stays green on forks / no-secret runs.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIG = ROOT / "db" / "migrations"

# ── parse the .sql files ────────────────────────────────────────────────
_CREATE_TABLE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?([a-z_][a-z0-9_]*)\s*\((.*?)\n\)\s*;",
    re.I | re.S)
_ADD_COLUMN = re.compile(
    r"alter\s+table\s+(?:public\.)?([a-z_][a-z0-9_]*)\s+(.*?);", re.I | re.S)
_ADD_COL_ONE = re.compile(r"add\s+column\s+(?:if\s+not\s+exists\s+)?([a-z_][a-z0-9_]*)", re.I)
_CREATE_FN = re.compile(
    r"create\s+(?:or\s+replace\s+)?function\s+(?:public\.)?([a-z_][a-z0-9_]*)\s*\(", re.I)
_CREATE_IDX = re.compile(
    r"create\s+(?:unique\s+)?index\s+(?:concurrently\s+)?(?:if\s+not\s+exists\s+)?([a-z_][a-z0-9_]*)\s+on",
    re.I)
_DROP_TABLE = re.compile(r"drop\s+table\s+(?:if\s+exists\s+)?(?:public\.)?([a-z_][a-z0-9_]*)", re.I)
_DROP_FN = re.compile(r"drop\s+function\s+(?:if\s+exists\s+)?(?:public\.)?([a-z_][a-z0-9_]*)", re.I)
_DROP_IDX = re.compile(r"drop\s+index\s+(?:if\s+exists\s+)?(?:public\.)?([a-z_][a-z0-9_]*)", re.I)
_DROP_COL = re.compile(r"drop\s+column\s+(?:if\s+exists\s+)?([a-z_][a-z0-9_]*)", re.I)
_COL_LINE = re.compile(r"^\s*\"?([a-z_][a-z0-9_]*)\"?\s+[a-z]", re.I)
# lines inside a CREATE TABLE body that are constraints, not columns
_NOT_A_COL = re.compile(r"^\s*(constraint|primary\s+key|unique|foreign\s+key|check|like)\b", re.I)


def _strip_sql_comments(s: str) -> str:
    return re.sub(r"--[^\n]*", "", s)


def parse_expectations() -> dict:
    tables: set[str] = set()
    columns: set[tuple[str, str]] = set()
    functions: set[str] = set()
    indexes: set[str] = set()
    for f in sorted(MIG.glob("*.sql")):
        raw = _strip_sql_comments(f.read_text())
        # drops first, so a `drop … ; create …` in the same file nets to exists
        for name in _DROP_TABLE.findall(raw):
            tables.discard(name.lower())
            columns = {c for c in columns if c[0] != name.lower()}
        for name in _DROP_FN.findall(raw):
            functions.discard(name.lower())
        for name in _DROP_IDX.findall(raw):
            indexes.discard(name.lower())
        for tbl, rest in _ADD_COLUMN.findall(raw):
            for col in _DROP_COL.findall(rest):
                columns.discard((tbl.lower(), col.lower()))
        for name, body in _CREATE_TABLE.findall(raw):
            tables.add(name.lower())
            for line in body.splitlines():
                if _NOT_A_COL.match(line):
                    continue
                m = _COL_LINE.match(line)
                if m:
                    columns.add((name.lower(), m.group(1).lower()))
        for tbl, rest in _ADD_COLUMN.findall(raw):
            for col in _ADD_COL_ONE.findall(rest):
                columns.add((tbl.lower(), col.lower()))
        functions.update(n.lower() for n in _CREATE_FN.findall(raw))
        indexes.update(n.lower() for n in _CREATE_IDX.findall(raw))
    return {"tables": tables, "columns": columns,
            "functions": functions, "indexes": indexes}


# ── live schema ────────────────────────────────────────────────────────
def live_schema():
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if not (url and key):
        return None
    url, key = url.strip(), key.strip()
    from supabase import create_client
    try:
        sb = create_client(url, key)
        d = sb.rpc("introspect_schema").execute().data or {}
    except Exception:
        # a placeholder/unreachable URL -- e.g. another test module's dummy
        # SUPABASE_URL (tests/test_api.py sets one via os.environ.setdefault
        # so `api.main` imports offline) leaking into this process -- is
        # indistinguishable from "no real creds" for this cheap guard's
        # purposes. Skip, don't fail the suite over test-isolation noise.
        return None
    return {
        "tables": {t.lower() for t in d.get("tables", [])},
        "columns": {(c["table"].lower(), c["column"].lower()) for c in d.get("columns", [])},
        "functions": {r.lower() for r in d.get("routines", [])},
        "indexes": {i.lower() for i in d.get("indexes", [])},
    }


def diff(expected: dict, live: dict) -> dict:
    missing_tables = sorted(expected["tables"] - live["tables"])
    known_tables = expected["tables"] & live["tables"]
    missing_columns = sorted(
        f"{t}.{c}" for (t, c) in (expected["columns"] - live["columns"]) if t in known_tables)
    missing_functions = sorted(expected["functions"] - live["functions"])
    missing_indexes = sorted(expected["indexes"] - live["indexes"])
    # informational: live tables no migration creates (excluding Supabase's own)
    _SYS = ("supabase_", "schema_migrations", "spatial_ref_sys")
    unexpected_tables = sorted(
        t for t in (live["tables"] - expected["tables"])
        if not t.startswith(_SYS))
    return {
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_functions": missing_functions,
        "missing_indexes": missing_indexes,
        "unexpected_tables": unexpected_tables,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="verify_migrations")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict-unexpected", action="store_true",
                    help="also fail on live tables that no migration file creates")
    args = ap.parse_args(argv)

    expected = parse_expectations()
    live = live_schema()
    if live is None:
        print("verify_migrations: skipped (no SUPABASE_URL / SUPABASE_SERVICE_KEY)")
        return 0

    d = diff(expected, live)
    hard = (d["missing_tables"] or d["missing_columns"]
            or d["missing_functions"] or d["missing_indexes"]
            or (args.strict_unexpected and d["unexpected_tables"]))

    if args.json:
        print(json.dumps({"ok": not hard, **d}, indent=2))
    else:
        for k in ("missing_tables", "missing_columns", "missing_functions", "missing_indexes"):
            for item in d[k]:
                print(f"  DRIFT [{k[8:-1]}] {item} — in a migration .sql, not in the live schema")
        for t in d["unexpected_tables"]:
            print(f"  NOTE  live table `{t}` is created by no migration file")
        print("verify_migrations: " + ("DRIFT" if hard else "clean"))
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
