"""
P8c — human-reply review precision (KIL-c).

KIL-c runs `integrity.check(reply, contexts, kind="human_reply")` on every
*sent* human reply and opens a manager review when the result is `flagged`
(a contradiction) or `novel` (a materially new commitment). That decision is
binary — flag / don't-flag — and its precision is the manager's alert-fatigue
number: too many false flags and managers rubber-stamp.

For each labelled reply in cases.jsonl we take `pred_flag = flagged or novel`
and compare to `gold_flag`. We report precision / recall / F1 of the FLAG
class plus a 2x2 confusion matrix.

Runs against whichever backend `integrity` picks: the Groq NLI judge when
GROQ_API_KEY is set (the real number), else the deterministic heuristic (a
floor — expect lower recall on the subtler `novel` cases).

    python eval/review/run_review_eval.py
    python eval/review/run_review_eval.py --json
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

CASES = pathlib.Path(__file__).with_name("cases.jsonl")


def evaluate() -> dict:
    rows = [json.loads(ln) for ln in CASES.read_text().splitlines() if ln.strip()]
    tp = fp = tn = fn = 0
    misses = []
    for r in rows:
        res = integrity.check(r["reply"], r["contexts"], kind="human_reply")
        pred = bool(res.get("flagged") or res.get("novel"))
        gold = bool(r["gold_flag"])
        if pred and gold:
            tp += 1
        elif pred and not gold:
            fp += 1
            misses.append({"id": r["id"], "kind": "false flag", "backend": res["backend"]})
        elif not pred and gold:
            fn += 1
            misses.append({"id": r["id"], "kind": "missed", "backend": res["backend"]})
        else:
            tn += 1

    n = len(rows)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {
        "n": n,
        "backend": "groq" if llm.available() else "heuristic",
        "accuracy": round((tp + tn) / n, 3),
        "flag_precision": round(prec, 3),
        "flag_recall": round(rec, 3),
        "flag_f1": round(f1, 3),
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "misses": misses,
    }


def _print(r: dict) -> None:
    c = r["confusion"]
    print(f"\nhuman-reply review eval — {r['n']} cases — backend: {r['backend']}\n")
    print(f"  accuracy         {r['accuracy']:.3f}")
    print(f"  flag precision   {r['flag_precision']:.3f}   (1 - manager alert-fatigue rate)")
    print(f"  flag recall      {r['flag_recall']:.3f}   (bad replies caught)")
    print(f"  flag F1          {r['flag_f1']:.3f}")
    print(f"\n  confusion   flag&bad={c['tp']}  falseflag={c['fp']}  "
          f"clean={c['tn']}  missed={c['fn']}")
    if r["misses"]:
        print("\n  misses:")
        for m in r["misses"]:
            print(f"    {m['id']}: {m['kind']}")
    print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="run_review_eval")
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
