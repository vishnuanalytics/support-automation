"""
End-to-end action eval — does the flow make the right auto-send / escalate
decision, and where should the confidence threshold sit?

For each case in cases.jsonl we run the REAL pipeline
(`build_graph(flow).invoke`), compare `outcome.action` to `gold_action`,
and report:

  action accuracy        pred == gold
  auto-send precision     of cases the bot auto-replied, fraction where gold
                          was also auto_reply  (proxy for "safe to send")
  escalation precision    of cases the bot escalated, fraction where gold
                          was not auto_reply   (proxy for "needed a human")
  coverage               fraction auto-replied

Then a threshold sweep: using each run's recorded retrieval_score /
draft_confidence / groundedness / tier, re-derive the decision at a range
of `default_threshold` values (keeping the tier→handover rule) and print
the precision / coverage curve — the calibration output.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (+ optional GROQ_API_KEY for real
drafts). Slow — real embed + rerank per case.

    python eval/e2e/run_e2e.py [--flow <id>] [--tenant <id> --team support]
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from dotenv import load_dotenv

load_dotenv()
os.environ["RUNS_DISABLED"] = "1"  # don't pollute the runs table with eval runs

from interpreter.builder import build_graph  # noqa: E402
from interpreter.loader import load_flow  # noqa: E402

CASES = pathlib.Path(__file__).with_name("cases.jsonl")
ACME_SUPPORT = "11111111-1111-1111-1111-111111111111"
ACTIONS = ("auto_reply", "ask_human", "handover")


def load_cases() -> list[dict]:
    return [json.loads(l) for l in CASES.read_text().splitlines() if l.strip()]


def run_case(graph, c: dict) -> dict:
    case = {"case_id": c["id"], "subject": c["subject"], "body": c["body"], "account": c["account"]}
    f = graph.invoke({"case": case, "trace": []})
    gate = f.get("confidence_gate") or {}
    toks = sum(
        (s["data"].get("tokens") or {}).get("total", 0)
        for s in f.get("trace", []) if isinstance(s.get("data"), dict)
    )
    ms = sum(s["data"].get("elapsed_ms", 0) for s in f.get("trace", []) if isinstance(s.get("data"), dict))
    return {
        "id": c["id"],
        "gold": c["gold_action"],
        "pred": (f.get("outcome") or {}).get("action"),
        "tier": f.get("tier"),
        "retrieval_score": gate.get("retrieval_score", 0.0),
        "draft_confidence": gate.get("draft_confidence", 0.0),
        "groundedness": gate.get("groundedness", 0.0),
        "score": gate.get("score"),
        "tokens": toks,
        "elapsed_ms": round(ms, 1),
    }


def _prf(rows: list[dict]) -> None:
    n = len(rows)
    acc = sum(r["pred"] == r["gold"] for r in rows) / n
    auto = [r for r in rows if r["pred"] == "auto_reply"]
    esc = [r for r in rows if r["pred"] in ("ask_human", "handover")]
    auto_p = sum(r["gold"] == "auto_reply" for r in auto) / len(auto) if auto else float("nan")
    esc_p = sum(r["gold"] != "auto_reply" for r in esc) / len(esc) if esc else float("nan")
    print(f"  cases                {n}")
    print(f"  action accuracy      {acc:6.3f}")
    print(f"  auto-send precision  {auto_p:6.3f}   ({sum(r['gold']=='auto_reply' for r in auto)}/{len(auto)})")
    print(f"  escalation precision {esc_p:6.3f}   ({sum(r['gold']!='auto_reply' for r in esc)}/{len(esc)})")
    print(f"  coverage (auto)      {len(auto)/n:6.3f}   ({len(auto)}/{n})")
    lat = sorted(r["elapsed_ms"] for r in rows)
    print(f"  latency p50 / p95    {lat[n//2]:.0f} / {lat[int(n*0.95)-1]:.0f} ms"
          + (f"   tokens/run avg {sum(r['tokens'] for r in rows)/n:.0f}" if any(r["tokens"] for r in rows) else "  (stub — 0 tokens)"))


def _confusion(rows: list[dict]) -> None:
    print("\n  confusion (rows=gold, cols=pred):")
    print("            " + "".join(f"{a:>12}" for a in ACTIONS))
    for g in ACTIONS:
        line = "".join(f"{sum(r['gold']==g and r['pred']==p for r in rows):>12}" for p in ACTIONS)
        print(f"    {g:>8}{line}")
    bad = [r for r in rows if r["pred"] != r["gold"]]
    if bad:
        print(f"\n  {len(bad)} mismatch(es):")
        for r in bad:
            print(f"    [{r['id']}] gold={r['gold']:<10} pred={r['pred']:<10} "
                  f"tier={r['tier']:<10} score={r['score']} (retr {r['retrieval_score']:.2f} / "
                  f"draft {r['draft_confidence']:.2f} / grnd {r['groundedness']:.2f})")


def _sweep(rows: list[dict]) -> None:
    """Re-derive the decision at each candidate default_threshold, keeping the
    tier==enterprise -> handover rule. Non-enterprise: auto_reply iff score>=t."""
    print("\n  threshold sweep (uniform default_threshold, tier rule kept):")
    print(f"    {'t':>5} {'auto_p':>8} {'esc_p':>8} {'coverage':>10} {'acc':>7}")
    for i in range(3, 19):
        t = i / 20  # 0.15 .. 0.90
        preds = []
        for r in rows:
            if r["tier"] == "enterprise":
                preds.append("handover")
            elif (r["score"] or 0.0) >= t:
                preds.append("auto_reply")
            else:
                preds.append("ask_human")
        sim = [{**r, "pred": p} for r, p in zip(rows, preds)]
        auto = [x for x in sim if x["pred"] == "auto_reply"]
        esc = [x for x in sim if x["pred"] in ("ask_human", "handover")]
        ap = sum(x["gold"] == "auto_reply" for x in auto) / len(auto) if auto else float("nan")
        ep = sum(x["gold"] != "auto_reply" for x in esc) / len(esc) if esc else float("nan")
        acc = sum(x["pred"] == x["gold"] for x in sim) / len(sim)
        print(f"    {t:>5.2f} {ap:>8.3f} {ep:>8.3f} {len(auto)/len(sim):>10.3f} {acc:>7.3f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--flow", default=ACME_SUPPORT)
    ap.add_argument("--tenant")
    ap.add_argument("--team")
    args = ap.parse_args()

    flow = (
        load_flow(tenant_id=args.tenant, team=args.team, status="published")
        if args.tenant and args.team
        else load_flow(flow_id=args.flow)
    )
    graph = build_graph(flow)
    cases = load_cases()
    print(f"flow: {flow['name']}  ·  {len(cases)} cases\n")

    rows = []
    for c in cases:
        r = run_case(graph, c)
        rows.append(r)
        flag = " " if r["pred"] == r["gold"] else "✗"
        print(f"  {flag} [{r['id']}] gold={r['gold']:<10} pred={r['pred']:<10} score={r['score']}")

    print("\n" + "=" * 60)
    _prf(rows)
    _confusion(rows)
    _sweep(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
