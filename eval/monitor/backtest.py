"""Backtest Case Monitor's AI drafts against what agents really sent.

For a resolved case, hide an agent's reply, draft it from the earlier
conversation, then score the draft. The case is left out of retrieval, or the
model could just copy the answer.

    python -m eval.monitor.backtest --tenant <id> --cases 15 --out /tmp/run.json

The output holds case text, so keep it out of the repository.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
from typing import Any

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("ERROR_REPORTING", "NO")
os.environ.setdefault("DEEPEVAL_DISABLE_PROGRESS_BAR", "YES")
os.environ.setdefault("CONFIDENT_TRACE_FLUSH", "NO")

_ETA = re.compile(r"\b(within|by)\s+\d+\s*(hour|hr|day|business day|minute)s?\b|"
                  r"\bby (today|tomorrow|eod|end of day|monday|tuesday|wednesday|thursday|friday)\b",
                  re.I)
_RCA = re.compile(r"\b(will|shall) (share|provide|send) (you )?(the |an )?rca\b", re.I)
_IDS = re.compile(r"\b\d{5,}\b")
_MARKER = re.compile(r"\[\d+\]")

# The prompt before anchoring on the latest message, kept so runs can be compared.
OLD_PROMPT = (
    "You help a restaurant software company's support lead. Using only the numbered sources, "
    "write a reply for the requested audience. Past cases show how similar issues went: use them "
    "for approach, but never state their details as facts about this case. Never promise an ETA "
    "or an RCA unless a source commits to one. Keep any placeholder like [email] or [number] "
    "exactly as written. Cite sources only in situation, never in the draft. If a needed fact is "
    "missing, list it under missing. Answer as JSON: "
    "{\"situation\": two or three sentences on where this case stands, citing sources like [1], "
    "\"draft\": the message, \"missing\": [facts to fill in before sending]}."
)

COMPARE = (
    "Compare the actual output with the expected output, which is the reply a human support "
    "agent really sent at this point. Pass if the draft takes the same next step: it asks for the "
    "same thing, shares the same update, or gives the same resolution. Different wording is fine. "
    "Fail if it takes a different step, misses the main point, or only gives a generic follow-up "
    "when the agent said something specific."
)
ACTIONABLE = (
    "Judge the actual output as a message to send now. Pass if it's specific to this case, says "
    "what is needed or what happened, and reads as professional. Fail if it's vague, repeats the "
    "input without moving things forward, or promises a date or root cause that the context "
    "doesn't commit to."
)


def strict_checks(draft: str, context: list[str], case_context: list[str]) -> dict[str, bool]:
    """Checks that need no judge. True means the check passed."""
    this_case = " ".join(case_context)
    leaked = [i for i in _IDS.findall(draft) if i not in this_case
              and any(i in c for c in context if c not in case_context)]
    return {"no_source_markers": not _MARKER.search(draft),
            "no_eta_promise": not _ETA.search(draft),
            "no_rca_promise": not _RCA.search(draft),
            "no_ids_from_other_cases": not leaked,
            "not_empty": len(draft.strip()) > 20}


def pick_pivot(case: dict, cfg: dict) -> int | None:
    """The last agent reply that answers someone else, if there's history before it."""
    from interpreter.monitor import knowledge, verdict

    messages = sorted(case["messages"], key=lambda m: m.get("at") or "")
    case["messages"] = messages
    for i in range(len(messages) - 1, 0, -1):
        m = messages[i]
        party = verdict.email_party(m.get("from"), bool(m.get("incoming")), case, cfg)
        prior = verdict.email_party(messages[i - 1].get("from"),
                                    bool(messages[i - 1].get("incoming")), case, cfg)
        if party == verdict.US and prior != verdict.US and \
                len(knowledge.message_text(m.get("text"))) > 60:
            return i
    return None


def audience_of(message: dict, case: dict) -> str:
    from interpreter.monitor import verdict

    partner = case.get("partner") or {}
    to = re.findall(r"@([\w.-]+\.\w+)", message.get("to") or "")
    return "aggregator" if any(verdict.in_domains(d, partner.get("email_domains") or [])
                               for d in to) else "merchant"


def to_timeline(case: dict, messages: list[dict], cfg: dict) -> list[dict]:
    from interpreter.monitor import knowledge, timeline

    raw = {"emails": [{"from": m.get("from"), "text": knowledge.strip_quoted(m.get("text")),
                       "at": m.get("at"), "incoming": m.get("incoming")} for m in messages]}
    return timeline.merge(case, raw, [], cfg)


def score(row: dict, judge: Any) -> dict[str, Any]:
    from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric, GEval
    from deepeval.test_case import LLMTestCase, LLMTestCaseParams as P

    tc = LLMTestCase(input=row["input"], actual_output=row["draft"], expected_output=row["actual"],
                     retrieval_context=row["context"], context=row["context"])
    metrics = {
        "faithfulness": FaithfulnessMetric(threshold=0.7, model=judge, include_reason=True,
                                           async_mode=False),
        "relevancy": AnswerRelevancyMetric(threshold=0.7, model=judge, include_reason=True,
                                           async_mode=False),
        "same_next_step": GEval(name="SameNextStep", criteria=COMPARE, threshold=0.6, model=judge,
                                evaluation_params=[P.INPUT, P.ACTUAL_OUTPUT, P.EXPECTED_OUTPUT],
                                async_mode=False),
        "actionable": GEval(name="Actionable", criteria=ACTIONABLE, threshold=0.6, model=judge,
                            evaluation_params=[P.INPUT, P.ACTUAL_OUTPUT, P.CONTEXT],
                            async_mode=False),
    }
    out: dict[str, Any] = {}
    for name, metric in metrics.items():
        try:
            metric.measure(tc, _show_indicator=False)
            out[name] = {"score": round(float(metric.score or 0), 3),
                         "reason": (metric.reason or "")[:300]}
        except Exception as e:  # noqa: BLE001 -- one judge failure shouldn't lose the run
            out[name] = {"error": type(e).__name__}
    return out


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(".env")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--cases", type=int, default=15)
    parser.add_argument("--since", default="2026-08-01")
    parser.add_argument("--until", default="2026-11-01")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", required=True)
    parser.add_argument("--judge", default=None)
    parser.add_argument("--no-score", action="store_true")
    parser.add_argument("--old-prompt", action="store_true")
    args = parser.parse_args()
    logging.disable(logging.WARNING)

    from eval.deepeval.groq_model import GroqJudge
    from ingestion.scraper import get_supabase
    from interpreter.monitor import knowledge, service
    from interpreter.monitor.store import SupabaseStore

    sb = get_supabase()
    cfg = service.load_config(sb, args.tenant)
    reader = service._case_reader(sb, args.tenant, cfg)
    partners = SupabaseStore(sb).partners(args.tenant)
    index = {n.lower(): p for p in partners for n in [p["name"], *(p.get("aliases") or [])]}
    cases = list(reader.resolved_conversations(cfg["dependency_record_type"],
                                               f"{args.since}T00:00:00Z", f"{args.until}T00:00:00Z"))
    random.Random(args.seed).shuffle(cases)
    judge = None if args.no_score else GroqJudge(args.judge)

    rows: list[dict] = []
    for case in cases:
        if len(rows) >= args.cases:
            break
        case["partner"] = next((index[n.lower()] for n in case["third_parties"]
                                if n.lower() in index), None)
        pivot = pick_pivot(case, cfg)
        if pivot is None:
            continue
        hidden = case["messages"][pivot]
        history = to_timeline(case, case["messages"][:pivot], cfg)
        audience = audience_of(hidden, case)
        try:
            got = knowledge.generate_draft(args.tenant, case, audience, history, sb, case["partner"],
                                           exclude_case=case["id"],
                                           system=OLD_PROMPT if args.old_prompt else None)
        except Exception as e:  # noqa: BLE001
            print("draft failed", case["case_number"], type(e).__name__, flush=True)
            continue
        history_text = [knowledge.message_text(t["text"]) for t in history]
        row = {"case": case["case_number"], "audience": audience,
               "partner": (case["partner"] or {}).get("name"), "input": history_text[-1][:1500],
               "draft": got["draft"], "draft_chars": len(got["draft"]),
               "actual_chars": len(knowledge.message_text(hidden.get("text"))), "situation": got["situation"], "missing": got["missing"],
               "actual": knowledge.message_text(hidden.get("text"))[:1500],
               "context": got["context"], "similar_used": got["similar_used"]}
        row["strict"] = strict_checks(got["draft"], got["context"], history_text)
        if judge:
            row["scores"] = score(row, judge)
        rows.append(row)
        with open(args.out, "w") as f:
            json.dump(rows, f, indent=1)
        print(len(rows), row["case"], row["audience"],
              {k: v.get("score") for k, v in (row.get("scores") or {}).items()},
              [k for k, v in row["strict"].items() if not v], flush=True)
    print(json.dumps(summary(rows), indent=1))
    return 0


def summary(rows: list[dict]) -> dict[str, Any]:
    thresholds = {"faithfulness": 0.7, "relevancy": 0.7, "same_next_step": 0.6, "actionable": 0.6}
    out: dict[str, Any] = {"cases": len(rows)}
    for name, limit in thresholds.items():
        vals = [r["scores"][name]["score"] for r in rows
                if "score" in (r.get("scores") or {}).get(name, {})]
        if vals:
            out[name] = {"mean": round(sum(vals) / len(vals), 3),
                         "pass": f"{sum(v >= limit for v in vals)}/{len(vals)}"}
    if rows:
        out["strict_failures"] = {k: sum(not r["strict"][k] for r in rows) for k in rows[0]["strict"]}
    return out


if __name__ == "__main__":
    sys.exit(main())
