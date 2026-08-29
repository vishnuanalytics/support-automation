"""
Phase 6 — conflicting-SOP detection across teams.

Different teams' flows can point their `retrieve` node at the corpus with
different settings (top_k, sparse/graph on/off, section filters later). This
script probes each team's retrieval with a fixed set of support topics and
flags where two teams surface *different* top documents for the same
question — then, if a Groq key is present, asks whether the two snippets
actually give conflicting guidance.

    python scripts/sop_conflicts.py

Needs Supabase (.env). Uses the local embed + rerank models (slow-ish).
Without GROQ_API_KEY the divergences are still reported, just unjudged.
"""

from __future__ import annotations

import json
import pathlib
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from interpreter import llm  # noqa: E402
from interpreter.loader import list_flows, load_flow  # noqa: E402
from interpreter.retrieval import hybrid_retrieve  # noqa: E402

TOPICS = [
    "How do I authenticate my Zapier integration?",
    "How do webhook / REST hook triggers work?",
    "How do I handle pagination in a trigger?",
    "What are the API rate limits and throttling rules?",
    "How do I test my integration's triggers and actions?",
    "How do I deprecate or migrate an old integration version?",
    "How do I deal with action timeouts hitting the 30-second limit?",
    "How does deduplication work for polling triggers?",
]


def team_retrieve_config() -> dict[str, dict]:
    """One representative published flow per team -> its retrieve node config."""
    out: dict[str, dict] = {}
    for meta in list_flows(status="published"):
        if meta["team"] in out:
            continue
        flow = load_flow(flow_id=meta["flow_id"], validate=False)
        rn = next((n for n in flow["nodes"] if n["type"] == "retrieve"), None)
        out[meta["team"]] = (rn or {}).get("config", {}) if rn else {}
    return out


def top_hit(query: str, cfg: dict) -> tuple[str, str]:
    src = cfg.get("source", ["supabase"])
    results, _ = hybrid_retrieve(
        query,
        top_k=1,
        use_sparse=cfg.get("use_sparse", True),
        use_graph=cfg.get("use_graph", "neo4j" in src),
        use_rerank=cfg.get("use_rerank", True),
    )
    if not results:
        return "", ""
    return results[0]["doc_url"], results[0]["chunk_text"][:900]


def judge(topic: str, a: tuple[str, str, str], b: tuple[str, str, str]) -> dict:
    if not llm.available():
        return {"conflict": None, "reason": "not judged (no GROQ_API_KEY)"}
    raw = llm.complete(
        system=(
            "You compare two documentation snippets that two different support "
            "teams' bots would use to answer the SAME customer question. Return "
            'JSON {"conflict": boolean, "reason": string}. conflict=true only if '
            "following one snippet would lead a customer to a materially different "
            "or contradictory action than the other."
        ),
        user=(
            f"# Question\n{topic}\n\n"
            f"# Team {a[0]} — {a[1]}\n{a[2]}\n\n"
            f"# Team {b[0]} — {b[1]}\n{b[2]}"
        ),
        model=llm.FAST_MODEL,
        json_object=True,
        max_tokens=250,
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"conflict": None, "reason": "judge returned non-JSON"}


def main() -> int:
    cfgs = team_retrieve_config()
    teams = sorted(cfgs)
    print(f"teams: {teams}\n")
    if len(teams) < 2:
        print("need >=2 teams with a published flow to compare; nothing to do.")
        return 0

    divergences = 0
    conflicts = 0
    for topic in TOPICS:
        hits = {t: top_hit(topic, cfgs[t]) for t in teams}
        urls = {t: h[0] for t, h in hits.items()}
        distinct = set(u for u in urls.values() if u)
        if len(distinct) < 2:
            continue
        divergences += 1
        pairs = [(teams[i], teams[j]) for i in range(len(teams)) for j in range(i + 1, len(teams))]
        print(f"▶ {topic}")
        for t, (u, _) in hits.items():
            print(f"    {t:<12} → {u.replace('https://docs.zapier.com', '') or '(none)'}")
        for ta, tb in pairs:
            if urls[ta] and urls[tb] and urls[ta] != urls[tb]:
                v = judge(topic, (ta, urls[ta], hits[ta][1]), (tb, urls[tb], hits[tb][1]))
                mark = "⚠ CONFLICT" if v.get("conflict") else ("· ok" if v.get("conflict") is False else "· ?")
                if v.get("conflict"):
                    conflicts += 1
                print(f"      {ta} vs {tb}: {mark} — {v.get('reason', '')}")
        print()

    print(f"summary: {divergences} topic(s) where teams retrieved different top docs; "
          f"{conflicts} judged as conflicting"
          + ("" if llm.available() else "  (judging skipped — no GROQ_API_KEY)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
