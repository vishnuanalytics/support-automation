"""
Phase 16 — structured policy rules.

A rule is `{when: <predicate>, then: <outcome>}`, authored in the UI as
plain JSON (never code). `when` is a predicate tree evaluated against the
whole flow `state`; `then` either overrides routing (`route`) or dispatches
an internal task (`task`).

    predicate := {"all": [predicate, ...]}
               | {"any": [predicate, ...]}
               | {"not": predicate}
               | {"field": "<dotted.path>", "op": "<op>", "value": <json>}

ops: eq ne in nin gt gte lt lte contains icontains exists

Everything here is pure + side-effect-free and unit-tested offline.
"""

from __future__ import annotations

from typing import Any

_MISSING = object()


def _dig(state: dict, dotted: str) -> Any:
    cur: Any = state
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


def _cmp(op: str, actual: Any, expected: Any) -> bool:
    if op == "exists":
        return (actual is not _MISSING) == bool(expected)
    if actual is _MISSING:
        return op in ("ne", "nin")
    try:
        if op == "eq":
            return actual == expected
        if op == "ne":
            return actual != expected
        if op == "in":
            return actual in (expected or [])
        if op == "nin":
            return actual not in (expected or [])
        if op == "gt":
            return float(actual) > float(expected)
        if op == "gte":
            return float(actual) >= float(expected)
        if op == "lt":
            return float(actual) < float(expected)
        if op == "lte":
            return float(actual) <= float(expected)
        if op == "contains":
            return expected in actual
        if op == "icontains":
            return str(expected).lower() in str(actual).lower()
    except (TypeError, ValueError):
        return False
    raise ValueError(f"unknown op {op!r}")


def evaluate(predicate: dict[str, Any] | None, state: dict) -> bool:
    """True if `predicate` matches `state`. An empty / None predicate never
    matches (a rule must be explicit about when it fires)."""
    if not predicate:
        return False
    if "all" in predicate:
        return all(evaluate(p, state) for p in predicate["all"])
    if "any" in predicate:
        return any(evaluate(p, state) for p in predicate["any"])
    if "not" in predicate:
        return not evaluate(predicate["not"], state)
    if "field" in predicate:
        return _cmp(predicate.get("op", "eq"), _dig(state, predicate["field"]),
                    predicate.get("value"))
    raise ValueError(f"malformed predicate: {predicate!r}")


def first_match(rules: list[dict[str, Any]], state: dict) -> dict[str, Any] | None:
    """`rules` sorted by ascending `priority` (0 = highest). Returns the first
    whose `when` matches, or None."""
    for rule in sorted(rules, key=lambda r: (r.get("priority", 100), r.get("name", ""))):
        if rule.get("status", "active") != "active":
            continue
        try:
            if evaluate(rule.get("when"), state):
                return rule
        except ValueError:
            continue   # a malformed rule never fires
    return None
