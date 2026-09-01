"""
Phase 23 — migration + seed-flow hygiene, for CI. No database needed.

Catches the class of drift we actually hit:
  * a gap or a duplicate in the NNN_ migration numbering
  * an empty / unreadable .sql file
  * a portable flow JSON that no longer compiles (build_graph / check_flow)
  * a flow whose confidence_gate edges aren't mutually exclusive-ish
    (every gate has a default or an obvious else)

    python -m scripts.check_migrations        # exit 0 clean, 1 on any problem
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _check_migrations() -> list[str]:
    d = ROOT / "db" / "migrations"
    nums: dict[int, str] = {}
    problems: list[str] = []
    for f in sorted(d.glob("*.sql")):
        m = re.match(r"^(\d{3})_", f.name)
        if not m:
            problems.append(f"{f.name}: no NNN_ prefix")
            continue
        n = int(m.group(1))
        if n in nums:
            problems.append(f"duplicate migration number {n:03d}: {nums[n]} + {f.name}")
        nums[n] = f.name
        if f.stat().st_size < 10:
            problems.append(f"{f.name}: empty")
    if nums:
        lo, hi = min(nums), max(nums)
        missing = [f"{i:03d}" for i in range(lo, hi + 1) if i not in nums]
        if missing:
            problems.append(f"gap in migration numbers: {', '.join(missing)}")
    return problems


def _check_flows() -> list[str]:
    sys.path.insert(0, str(ROOT))
    from interpreter.builder import build_graph
    from interpreter.flows.validate_flow import Flow, check_flow

    problems: list[str] = []
    for f in sorted((ROOT / "interpreter" / "flows").glob("flow_*.json")):
        try:
            flow = json.loads(f.read_text())
            errs = check_flow(Flow.model_validate(flow), require_expected_types=False)
            if errs:
                problems.append(f"{f.name}: check_flow -> {errs}")
            build_graph(flow)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{f.name}: {e.__class__.__name__}: {e}")
    return problems


def main() -> int:
    problems = _check_migrations() + _check_flows()
    if problems:
        print("migration / flow check FAILED:")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("migrations + portable flows OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
