"""
Safe evaluator for edge `condition` expressions.

An edge's `condition` jsonb is either `{}` (unconditional) or
`{"if": "<expr>"}` where <expr> is a small boolean expression over the
graph state, e.g.:

    confidence_gate.pass and tier != 'enterprise'
    not confidence_gate.pass and tier != 'enterprise'
    tier == 'enterprise'
    classification.urgency in ('high', 'critical')

This is authored data (Phase 5 UI will write it), so it must never be
`eval()`-d. We parse to an AST and walk a strict whitelist: boolean ops,
`not`, comparisons, names, attribute/index access, and literals. Anything
else raises `ConditionError`.

Name resolution: bare names come from a flat context dict built by the
builder from the current state (`tier`, `region`, `confidence`,
`confidence_gate`, `classification`, `retrieval_score`, ...). A missing
attribute / key resolves to `None` (so `confidence_gate.pass` before the
gate has run is simply falsy) but a missing *top-level name* raises --
that's a flow-authoring bug, not a runtime state gap.
"""

from __future__ import annotations

import ast
import keyword
import re
from typing import Any


class ConditionError(ValueError):
    pass


# `confidence_gate.pass` is natural to write but `pass` is a Python keyword,
# so `a.pass` won't parse as an attribute. Rewrite `.keyword` -> `["keyword"]`
# (handled by our Subscript support) before parsing. `\b` after the keyword
# keeps `x.is_valid` etc. untouched.
_KW_ATTR = re.compile(r"\.\s*(" + "|".join(keyword.kwlist) + r")\b")


def _normalize(expr: str) -> str:
    return _KW_ATTR.sub(lambda m: f'["{m.group(1)}"]', expr)


_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp, ast.And, ast.Or,
    ast.UnaryOp, ast.Not,
    ast.Compare,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Name, ast.Load,
    ast.Attribute,
    ast.Subscript,
    ast.Constant,
    ast.List, ast.Tuple, ast.Set,
)


def _check(node: ast.AST) -> None:
    for child in ast.walk(node):
        if not isinstance(child, _ALLOWED_NODES):
            raise ConditionError(
                f"disallowed syntax in condition: {type(child).__name__}"
            )


def _resolve(node: ast.AST, ctx: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        if node.id not in ctx:
            raise ConditionError(f"unknown name in condition: {node.id!r}")
        return ctx[node.id]

    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [_resolve(el, ctx) for el in node.elts]

    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, ctx)
        return _getitem(base, node.attr)

    if isinstance(node, ast.Subscript):
        base = _resolve(node.value, ctx)
        key = _resolve(node.slice, ctx)
        return _getitem(base, key)

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _truthy(_resolve(node.operand, ctx))

    if isinstance(node, ast.BoolOp):
        vals = [_resolve(v, ctx) for v in node.values]
        if isinstance(node.op, ast.And):
            out: Any = True
            for v in vals:
                if not _truthy(v):
                    return v
                out = v
            return out
        # Or
        for v in vals:
            if _truthy(v):
                return v
        return vals[-1]

    if isinstance(node, ast.Compare):
        left = _resolve(node.left, ctx)
        for op, right_node in zip(node.ops, node.comparators):
            right = _resolve(right_node, ctx)
            if not _compare(op, left, right):
                return False
            left = right
        return True

    raise ConditionError(f"cannot evaluate node: {type(node).__name__}")


def _getitem(base: Any, key: Any) -> Any:
    if base is None:
        return None
    if isinstance(base, dict):
        return base.get(key)
    return getattr(base, str(key), None)


def _truthy(v: Any) -> bool:
    return bool(v)


def _compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    if isinstance(op, ast.In):
        return left in right
    if isinstance(op, ast.NotIn):
        return left not in right
    # ordered comparisons: None is uncomparable -> always False
    if left is None or right is None:
        return False
    if isinstance(op, ast.Lt):
        return left < right
    if isinstance(op, ast.LtE):
        return left <= right
    if isinstance(op, ast.Gt):
        return left > right
    if isinstance(op, ast.GtE):
        return left >= right
    raise ConditionError(f"unsupported comparison: {type(op).__name__}")


def evaluate(expr: str, ctx: dict[str, Any]) -> bool:
    """Evaluate a condition expression against `ctx`; returns a bool."""
    try:
        tree = ast.parse(_normalize(expr), mode="eval")
    except SyntaxError as e:
        raise ConditionError(f"could not parse condition {expr!r}: {e}") from e
    _check(tree)
    return _truthy(_resolve(tree.body, ctx))


def condition_names(expr: str) -> set[str]:
    """Top-level names referenced by an expression (for validation / docs)."""
    tree = ast.parse(_normalize(expr), mode="eval")
    _check(tree)
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
