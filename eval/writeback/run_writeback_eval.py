"""
P8c — does a KIL-d write-back actually fix the KB?

When a manager confirms a contradicting reply was correct, `kb_writeback.
draft_change` proposes new KB text. Two things must hold for the loop to be
worth running:

  resolution   the drafted body no longer contradicts the confirmed
               statement — `integrity.check(statement, [new_body])` is not
               `contradicts` (ideally `entails`).
  answer lift  a customer question answered from the NEW body should carry
               the corrected facts the OLD body lacked. We proxy this with
               gold-keyword coverage: coverage(new_body) - coverage(stale_kb).

We also sanity-check each case is real: the stale body must actually
contradict the statement (`pre == contradicts`); cases that don't are
reported and excluded from the resolution rate.

Backend: Groq when GROQ_API_KEY is set (draft_change uses the LLM and the
NLI judge). Without it, draft_change falls back to "new body = the
statement", so resolution is near-trivial and lift is a floor — the real
signal needs creds, same as the other evals.

    python eval/writeback/run_writeback_eval.py
    python eval/writeback/run_writeback_eval.py --json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from dotenv import load_dotenv

load_dotenv()

from interpreter import integrity, kb_writeback, llm  # noqa: E402

CASES = pathlib.Path(__file__).with_name("cases.jsonl")


def _coverage(text: str, keywords: list[str]) -> float:
    if not keywords:
        return 0.0
    t = (text or "").lower()
    return sum(1 for k in keywords if k.lower() in t) / len(keywords)


def evaluate() -> dict:
    rows = [json.loads(ln) for ln in CASES.read_text().splitlines() if ln.strip()]
    per = []
    for r in rows:
        # a real uuid in the ref so draft_change takes the `supersede` path
        # (the common KIL-d shape — an existing entry is being rewritten).
        eid = str(uuid.uuid5(uuid.NAMESPACE_URL, r["id"]))
        task_row = {
            "statement": r["statement"],
            "contexts": [{"ref": f"kb://11111111-1111-1111-1111-111111111111/{eid}",
                          "text": r["stale_kb"], "kind": "internal_kb"}],
            "verdict": {"salient": r.get("salient") or []},
        }
        change = kb_writeback.draft_change(task_row)
        new_body = change.get("body_md") or ""

        pre = integrity.check(
            r["statement"], [{"ref": "kb://stale", "text": r["stale_kb"], "kind": "internal_kb"}],
            kind="draft")["relation"]
        post = integrity.check(
            r["statement"], [{"ref": "kb://new", "text": new_body, "kind": "internal_kb"}],
            kind="draft")["relation"]

        per.append({
            "id": r["id"],
            "op": change.get("op"),
            "valid": pre == "contradicts",
            "resolved": post != "contradicts",
            "post_relation": post,
            "cov_stale": round(_coverage(r["stale_kb"], r["gold_keywords"]), 3),
            "cov_new": round(_coverage(new_body, r["gold_keywords"]), 3),
        })

    valid = [p for p in per if p["valid"]]
    n_res = sum(1 for p in valid if p["resolved"])
    lifts = [p["cov_new"] - p["cov_stale"] for p in per]
    return {
        "n": len(rows),
        "n_valid": len(valid),
        "backend": "groq" if llm.available() else "heuristic",
        "resolution_rate": round(n_res / len(valid), 3) if valid else None,
        "mean_answer_lift": round(statistics.mean(lifts), 3) if lifts else 0.0,
        "mean_cov_stale": round(statistics.mean(p["cov_stale"] for p in per), 3),
        "mean_cov_new": round(statistics.mean(p["cov_new"] for p in per), 3),
        "invalid_cases": [p["id"] for p in per if not p["valid"]],
        "per_case": per,
    }


def _print(r: dict) -> None:
    print(f"\nKB write-back eval — {r['n']} cases ({r['n_valid']} valid) — "
          f"backend: {r['backend']}\n")
    rr = r["resolution_rate"]
    print(f"  resolution rate   {rr if rr is None else f'{rr:.3f}'}   "
          "(drafted body no longer contradicts the correction)")
    print(f"  answer lift       {r['mean_answer_lift']:+.3f}   "
          f"(keyword coverage {r['mean_cov_stale']:.2f} -> {r['mean_cov_new']:.2f})")
    if r["invalid_cases"]:
        print(f"  invalid cases     {', '.join(r['invalid_cases'])} "
              "(stale KB did not contradict the statement — excluded)")
    print("\n  per case:")
    for p in r["per_case"]:
        mark = " " if (p["resolved"] or not p["valid"]) else "x"
        print(f"   {mark} [{p['id']}] op={p['op']:<9} post={p['post_relation']:<11} "
              f"cov {p['cov_stale']:.2f}->{p['cov_new']:.2f}"
              + ("" if p["valid"] else "  (invalid)"))
    print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="run_writeback_eval")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    r = evaluate()
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        _print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
