"""
deepeval draft-quality eval for the support pipeline.

Runs the REAL flow (`build_graph(flow).invoke`) on each case in
cases.jsonl, then scores the drafted reply with deepeval metrics — all
judged by Groq (`interpreter/llm.py`), not deepeval's default OpenAI
judge:

  faithfulness    every factual claim in the draft is supported by the
                  passages the draft was actually given (the
                  "confidently-hallucinated reply that sails through the
                  gate" bug — see interpreter/groundedness.py).
  relevancy       the draft actually addresses the customer's question.
  hallucination   the draft does not contradict the retrieved passages.
  quality         GEval: correct + actionable for a docs question;
                  defers to a human (states no policy/price/compliance
                  fact) for a billing / refund / pricing / compliance /
                  account-access question.

A case "fails" a metric when its score is on the wrong side of the
threshold — that failure list is the bug report.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, GROQ_API_KEY (all three needed
for a real run; without GROQ the pipeline drafts are the offline stub and
the judge falls back too, so the numbers are a floor, not a signal).

    python -m eval.deepeval.run_deepeval
    python -m eval.deepeval.run_deepeval --metrics faithfulness,quality --only d11,d12
    python -m eval.deepeval.run_deepeval --limit 4 --json /tmp/out.json
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

# deepeval: no telemetry, no Confident-AI calls, no progress bars.
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("ERROR_REPORTING", "NO")
os.environ.setdefault("DEEPEVAL_DISABLE_PROGRESS_BAR", "YES")
os.environ.setdefault("CONFIDENT_TRACE_FLUSH", "NO")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from eval.deepeval.groq_model import GroqJudge  # noqa: E402
from eval.deepeval.pipeline import (ACME_SUPPORT, build_graph, get_flow,  # noqa: E402
                                    run_case)

CASES = pathlib.Path(__file__).with_name("cases.jsonl")
ALL_METRICS = ("faithfulness", "relevancy", "hallucination", "quality")
DEFER_TERMS = ("billing", "refund", "pricing", "discount", "invoice", "proration",
               "compliance", "soc 2", "soc2", "account access", "locked out")

QUALITY_CRITERIA = (
    "You are grading a customer-support reply (Actual Output) to a customer "
    "message (Input), given the reference passages (Retrieval Context) and an "
    "'Expected Output' note describing what a good reply does.\n"
    "1. If the Input is about billing, refunds, invoices, proration, pricing, "
    "discounts, compliance/SOC 2, or account access, a GOOD reply does NOT "
    "state a definitive policy, number, or yes/no answer — it acknowledges the "
    "question and defers to a human specialist. Asserting a policy or amount "
    "here is a serious failure.\n"
    "2. Otherwise a GOOD reply directly and correctly resolves the question "
    "using only facts present in the Retrieval Context, with concrete, "
    "actionable steps, and matches the thrust of the Expected Output note.\n"
    "3. Any factual claim not supported by the Retrieval Context is a failure, "
    "whichever category the question is in.\n"
    "Score 1.0 for a reply that fully meets the applicable bar, 0.0 for one "
    "that clearly violates it."
)


def load_cases(path: pathlib.Path, only: set[str] | None, limit: int | None) -> list[dict]:
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    if only:
        rows = [r for r in rows if r["id"] in only]
    if limit:
        rows = rows[:limit]
    return rows


def build_metrics(names: list[str], judge: GroqJudge, th: dict) -> dict:
    from deepeval.metrics import (AnswerRelevancyMetric, FaithfulnessMetric,
                                  GEval, HallucinationMetric)
    from deepeval.test_case import LLMTestCaseParams as P

    m: dict = {}
    if "faithfulness" in names:
        m["faithfulness"] = FaithfulnessMetric(
            threshold=th["faithfulness"], model=judge, async_mode=False,
            include_reason=True, verbose_mode=False)
    if "relevancy" in names:
        m["relevancy"] = AnswerRelevancyMetric(
            threshold=th["relevancy"], model=judge, async_mode=False,
            include_reason=True, verbose_mode=False)
    if "hallucination" in names:
        m["hallucination"] = HallucinationMetric(
            threshold=th["hallucination"], model=judge, async_mode=False,
            include_reason=True, verbose_mode=False)
    if "quality" in names:
        m["quality"] = GEval(
            name="SupportAnswerQuality",
            criteria=QUALITY_CRITERIA,
            evaluation_params=[P.INPUT, P.ACTUAL_OUTPUT, P.RETRIEVAL_CONTEXT,
                               P.EXPECTED_OUTPUT],
            threshold=th["quality"], model=judge, async_mode=False,
            verbose_mode=False)
    return m


def _passed(name: str, score: float, th: dict) -> bool:
    # deepeval 4.2+: every metric (hallucination included) scores 1 = pass,
    # 0 = fail, and `threshold` is the minimum passing score.
    return score >= th[name]


def measure_case(row: dict, metrics: dict, th: dict) -> dict:
    from deepeval.test_case import LLMTestCase

    draft = row["draft"]
    ctx = row["contexts"]
    res: dict = {"scores": {}, "reasons": {}, "errors": {}, "skipped": []}

    if not draft:
        res["skipped"] = list(metrics)
        res["note"] = "pipeline produced no draft"
        return res

    tc = LLMTestCase(
        input=row["input"],
        actual_output=draft,
        expected_output=row.get("expected_output") or "",
        retrieval_context=ctx or ["(no passages were retrieved for this case)"],
        context=ctx or ["(no passages were retrieved for this case)"],
    )

    for name, metric in metrics.items():
        if name in ("faithfulness", "hallucination") and not ctx:
            res["skipped"].append(name)
            continue
        try:
            metric.measure(tc, _show_indicator=False)
            score = float(metric.score if metric.score is not None else 0.0)
            res["scores"][name] = round(score, 4)
            res["reasons"][name] = (metric.reason or "").strip()
        except Exception as e:  # noqa: BLE001 — a judge failure shouldn't kill the run
            res["errors"][name] = f"{type(e).__name__}: {e}"
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flow", default=ACME_SUPPORT)
    ap.add_argument("--tenant")
    ap.add_argument("--team")
    ap.add_argument("--cases", type=pathlib.Path, default=CASES)
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--metrics", default=",".join(ALL_METRICS),
                    help=f"subset of {','.join(ALL_METRICS)}")
    ap.add_argument("--judge-model", default=None, help="a model id from the llm roster")
    ap.add_argument("--faithfulness-threshold", type=float, default=0.7)
    ap.add_argument("--relevancy-threshold", type=float, default=0.7)
    ap.add_argument("--hallucination-threshold", type=float, default=0.8)
    ap.add_argument("--quality-threshold", type=float, default=0.6)
    ap.add_argument("--json", dest="json_out", type=pathlib.Path)
    ap.add_argument("--no-fail", action="store_true",
                    help="always exit 0 (report only)")
    ap.add_argument("--verbose", action="store_true", help="print full judge reasons")
    args = ap.parse_args()

    names = [n.strip() for n in args.metrics.split(",") if n.strip()]
    bad = set(names) - set(ALL_METRICS)
    if bad:
        ap.error(f"unknown metric(s): {sorted(bad)}")
    th = {
        "faithfulness": args.faithfulness_threshold,
        "relevancy": args.relevancy_threshold,
        "hallucination": args.hallucination_threshold,
        "quality": args.quality_threshold,
    }
    only = {s.strip() for s in args.only.split(",")} if args.only else None
    rows_in = load_cases(args.cases, only, args.limit)

    judge = GroqJudge(args.judge_model)
    metrics = build_metrics(names, judge, th)

    flow = get_flow(args.flow, args.tenant, args.team)
    graph = build_graph(flow)
    print(f"flow: {flow['name']}   judge: {judge.get_model_name()}")
    print(f"{len(rows_in)} case(s)   metrics: {', '.join(names)}")
    print("thresholds: " + "  ".join(f"{n}>={th[n]}" for n in names) + "\n")

    results = []
    for c in rows_in:
        row = run_case(graph, c)
        m = measure_case(row, metrics, th)
        results.append({**row, **m})

        cells = []
        for n in names:
            if n in m["scores"]:
                s = m["scores"][n]
                cells.append(f"{n[:5]} {s:.2f} {'ok ' if _passed(n, s, th) else 'FAIL'}")
            elif n in m["errors"]:
                cells.append(f"{n[:5]} ERR")
            else:
                cells.append(f"{n[:5]}  -  ")
        print(f"  [{row['id']}] gold={str(row['gold_action']):<10} "
              f"pred={str(row['pred_action']):<10} | " + " | ".join(cells))
        if args.verbose:
            for n in names:
                if m["reasons"].get(n):
                    print(f"       {n}: {m['reasons'][n]}")
                if m["errors"].get(n):
                    print(f"       {n} ERROR: {m['errors'][n]}")

    # ---- aggregate ----
    print("\n" + "=" * 70)
    failures: list[tuple[str, str, float, str]] = []
    errors: list[tuple[str, str, str]] = []
    for n in names:
        scored = [r for r in results if n in r["scores"]]
        pas = [r for r in scored if _passed(n, r["scores"][n], th)]
        line = (f"  {n:<14} {len(pas)}/{len(scored)} pass"
                if scored else f"  {n:<14} (no scored cases)")
        if scored:
            avg = sum(r["scores"][n] for r in scored) / len(scored)
            by = {}
            for r in scored:
                g = r["gold_action"] or "?"
                by.setdefault(g, []).append(_passed(n, r["scores"][n], th))
            split = "  ".join(f"{g}:{sum(v)}/{len(v)}" for g, v in sorted(by.items()))
            line += f"   avg {avg:.3f}   [{split}]"
        print(line)
        for r in scored:
            if not _passed(n, r["scores"][n], th):
                failures.append((r["id"], n, r["scores"][n], r["reasons"].get(n, "")))
        for r in results:
            if n in r["errors"]:
                errors.append((r["id"], n, r["errors"][n]))

    if failures:
        print(f"\n  {len(failures)} metric failure(s) — the bug report:")
        for cid, n, sc, why in failures:
            print(f"    [{cid}] {n} = {sc:.2f}")
            if why:
                print(f"        {why[:400]}")
    else:
        print("\n  no metric failures.")
    if errors:
        print(f"\n  {len(errors)} judge error(s):")
        for cid, n, e in errors:
            print(f"    [{cid}] {n}: {e[:200]}")

    if args.json_out:
        args.json_out.write_text(json.dumps(
            {"flow": flow["name"], "judge": judge.get_model_name(),
             "thresholds": th, "metrics": names, "results": results,
             "failures": [{"id": c, "metric": n, "score": s, "reason": w}
                          for c, n, s, w in failures],
             "errors": [{"id": c, "metric": n, "error": e} for c, n, e in errors]},
            indent=2))
        print(f"\n  wrote {args.json_out}")

    return 0 if (args.no_fail or not failures) else 1


if __name__ == "__main__":
    raise SystemExit(main())
