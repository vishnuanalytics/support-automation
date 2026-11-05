"""Similar resolved cases and duplicate grouping. Pure."""

from __future__ import annotations

from .links import github_key
from .verdict import parse_dt


def _names(value: list[str] | None) -> set[str]:
    return {v.lower() for v in value or []}


def rank(case: dict, pool: list[dict], limit: int = 5) -> list[dict]:
    """Same module, ranked by how many of module/sub-module/third party match,
    then by most recently closed."""
    module = (case.get("module") or "").lower()
    if not module:
        return []
    out: list[dict] = []
    for row in pool:
        if (row.get("module") or "").lower() != module or row.get("id") == case.get("id"):
            continue
        matches = 1
        sub = (case.get("sub_module") or "").lower()
        if sub and sub == (row.get("sub_module") or "").lower():
            matches += 1
        if _names(case.get("third_parties")) & _names(row.get("third_parties")):
            matches += 1
        opened, closed = parse_dt(row.get("opened_at")), parse_dt(row.get("closed_at"))
        hours = round((closed - opened).total_seconds() / 3600, 1) if opened and closed else None
        out.append({**row, "match_count": matches, "hours_to_resolve": hours})
    out.sort(key=lambda r: (r["match_count"],
                            t.timestamp() if (t := parse_dt(r.get("closed_at"))) else 0),
             reverse=True)
    return out[:limit]


def group_duplicates(cases: list[dict]) -> dict[str, list[dict]]:
    """Open cases pointing at the same GitHub issue: one incident, many cases."""
    groups: dict[str, list[dict]] = {}
    for case in cases:
        keys = {k for k in map(github_key, case.get("links") or []) if k}
        for key in keys:
            groups.setdefault(key, []).append(case)
    return {k: v for k, v in groups.items() if len(v) > 1}
