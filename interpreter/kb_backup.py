"""
Phase 28, step 6 of 6 — bulk KB export/import.

Pure shaping/validation only (no Supabase calls here — same split as
billing.py / kil_metrics.py: the route in api/main.py fetches, this module
shapes; api/worker.py's `_import_kb_bundle` does the actual writes).

Export produces the same shape import consumes, so a downloaded backup
round-trips: export a collection, re-import it (into the same or a
different collection) and get equivalent entries back.
"""

from __future__ import annotations

from typing import Any

# statuses worth backing up/restoring -- 'archived' is soft-deleted (skip);
# 'superseded' is a stale, deliberately-retired version (skip, matching what
# a "restore this collection" workflow would want: the current KB, not its
# history).
_EXPORT_STATUSES = ("active", "provisional")

MAX_IMPORT_ENTRIES = 500


def export_bundle(collection: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    """`entries` already fetched + filtered by the caller's DB query; this
    just shapes the JSON bundle a user downloads."""
    return {
        "collection": {
            "name": collection.get("name"),
            "description": (collection.get("config") or {}).get("description"),
        },
        "entries": [
            {"title": e.get("title"), "body_md": e.get("body_md") or "",
             "status": e.get("status", "active")}
            for e in entries
        ],
    }


def normalize_import_entries(raw: Any) -> tuple[list[dict[str, str]], list[str]]:
    """Untrusted `entries` from an uploaded bundle -> (clean entries,
    warnings). Never raises -- a malformed item is skipped with a warning,
    matching flow_candidate.assemble_candidate's "degrade, don't crash" style
    for untrusted JSON input."""
    warnings: list[str] = []
    if not isinstance(raw, list):
        return [], ["entries must be a list"]

    out: list[dict[str, str]] = []
    seen_titles: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            warnings.append(f"entry #{i}: not an object -- skipped")
            continue
        title = str(item.get("title") or "").strip()
        body = str(item.get("body_md") or "").strip()
        if not title or not body:
            warnings.append(f"entry #{i} ({title or '(no title)'}): missing title/body_md -- skipped")
            continue
        if title in seen_titles:
            warnings.append(f"entry #{i} ({title}): duplicate title in this bundle -- skipped")
            continue
        seen_titles.add(title)
        out.append({"title": title[:500], "body_md": body})
        if len(out) >= MAX_IMPORT_ENTRIES:
            warnings.append(f"bundle has more than {MAX_IMPORT_ENTRIES} entries -- the rest were dropped")
            break
    return out, warnings
