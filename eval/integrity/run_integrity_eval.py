"""
KIL-b — contradiction-judge eval.

For each labelled case in cases.jsonl we run `integrity.check(statement,
contexts, kind)` and compare the predicted relation to `gold`. We report:

  accuracy                pred == gold over all three classes
  flag precision / recall of the `contradicts` class — the class that
                          actually raises a manager review, so its precision
                          is the alert-fatigue number and its recall is the
                          missed-contradiction number
  confusion matrix        gold (rows) x pred (cols)

Runs against whichever backend `integrity` picks: the Groq NLI judge when
GROQ_API_KEY is set (the real number), else the deterministic heuristic (a
floor — expect lower recall).

    python eval/integrity/run_integrity_eval.py
    python eval/integrity/run_integrity_eval.py --json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from dotenv import load_dotenv

load_dotenv()

from interpreter import integrity, llm  # noqa: E402

_REL = ("entails", "neutral", "contradicts")
CASES = pathlib.Path(__file__).with_name("cases.jsonl")


def evaluate() -> dict:
    rows = [json.loads(ln) for ln in CASES.read_text().splitlines() if ln.strip()]
    conf = {g: {p: 0 for p in _REL} for g in _REL}
    misses = []
    for r in rows:
        res = integrity.check(r["statement"], r["contexts"], kind=r.get("kind", "draft"))
        pred = res["relation"]
        conf[r["gold"]][pred] += 1
        if pred != r["gold"]:
            misses.append({"id": r["id"], "gold": r["gold"], "pred": pred,
                           "backend": res["backend"]})

    n = len(rows)
    correct = sum(conf[g][g] for g in _REL)
    tp = conf["contradicts"]["contradicts"]
    fp = sum(conf[g]["contradicts"] for g in _REL if g != "contradicts")
    fn = sum(conf["contradicts"][p] for p in _REL if p != "contradicts")
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {
        "n": n,
        "backend": "groq" if llm.available() else "heuristic",
        "accuracy": round(correct / n, 3),
        "flag_precision": round(prec, 3),
        "flag_recall": round(rec, 3),
        "flag_f1": round(f1, 3),
        "confusion": conf,
        "misses": misses,
    }


def _print(r: dict) -> None:
    print(f"\nintegrity judge eval — {r['n']} cases — backend: {r['backend']}\n")
    print(f"  accuracy         {r['accuracy']:.3f}")
    print(f"  flag precision   {r['flag_precision']:.3f}   (1 - alert-fatigue rate)")
    print(f"  flag recall      {r['flag_recall']:.3f}   (contradictions caught)")
    print(f"  flag F1          {r['flag_f1']:.3f}")
    print("\n  confusion  gold \\ pred " + "  ".join(f"{p:>11}" for p in _REL))
    for g in _REL:
        print(f"  {g:>22}  " + "  ".join(f"{r['confusion'][g][p]:>11}" for p in _REL))
    if r["misses"]:
        print("\n  misses:")
        for m in r["misses"]:
            print(f"    {m['id']}: gold={m['gold']} pred={m['pred']}")
    print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="run_integrity_eval")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    r = evaluate()
    print(json.dumps(r, indent=2) if args.json else "", end="")
    if not args.json:
        _print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
