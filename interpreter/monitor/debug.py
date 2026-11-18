"""A debug-with-proof write-up for one case: root cause, the evidence behind it, what wasn't verified. Pure.

The model sees numbered sources only (the case's messages, GitHub and Slack text, and any saved
data checks). Its answer is parsed here, and a claim that doesn't cite a real source number is
moved to "not verified" rather than shown as proven. The same shape is what the background Claude
routine saves through the MCP `save_investigation` tool, so the case page shows both alike.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .redact import redact_pii

INVESTIGATION = "Investigation"  # check_name a saved write-up is stored under
SOURCE_LIMIT = 30
CHECK_ROWS_SHOWN = 10
TEXT_CAP = 600

DEBUG_SYSTEM = (
    "You are a senior support engineer helping a lead resolve one support case fast. Use ONLY the "
    "numbered sources given. Never use outside knowledge about the product to state a cause.\n"
    "Rules:\n"
    "- root_cause: one or two sentences. If the sources don't show a cause, say \"Not established\" "
    "and give the most likely line of enquiry in next_action.\n"
    "- evidence: one item per claim behind the root cause. Each has claim, source (the number of the "
    "source it comes from) and quote (the exact words or value copied from that source). Do not "
    "paraphrase the quote. Do not include a claim with no source.\n"
    "- not_verified: facts you would need and can't confirm from the sources (for example application "
    "logs, an order's status in the aggregator's dashboard).\n"
    "- next_action: who does what next, in one or two sentences.\n"
    "- draft_reply: a short reply to the party who spoke last, using only facts in evidence. If the root "
    "cause is not established, write a holding reply that asks for the missing detail. No citations in "
    "the draft. Keep placeholders like [email] exactly as written.\n"
    "- Saved data checks are real query results: quote their values exactly. A check with no rows "
    "means the query found nothing, not that the thing never happened.\n"
    "Answer as JSON: {\"root_cause\": str, \"evidence\": [{\"claim\": str, \"source\": int, \"quote\": str}], "
    "\"not_verified\": [str], \"next_action\": str, \"draft_reply\": str}."
)


def _cap(text: Any, n: int = TEXT_CAP) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n].rstrip() + "…"


def _check_text(check: dict) -> str:
    rows = check.get("rows") or []
    head = f"{check.get('check_name')}: {check.get('summary') or 'no summary'} ({check.get('row_count') or 0} rows)"
    if not rows:
        return head
    shown = json.dumps(rows[:CHECK_ROWS_SHOWN], default=str, ensure_ascii=False)
    return f"{head}. First rows: {_cap(shown, 900)}"


def build_sources(timeline: list[dict], checks: list[dict]) -> list[dict]:
    """Numbered sources: the latest conversation, then saved checks (the write-up itself is left out)."""
    talk = [t for t in timeline if t.get("party") != "system" and t.get("text")]
    out = [{"source": t.get("source") or "Message", "url": t.get("url"),
            "text": f"{t.get('party_label') or t.get('party')} ({(t.get('at') or '')[:16]}): "
                    f"{_cap(redact_pii(t['text']))}"} for t in talk[-SOURCE_LIMIT:]]
    for c in checks:
        if c.get("check_name") != INVESTIGATION:
            out.append({"source": f"Saved check: {c.get('check_name')}", "url": None,
                        "text": redact_pii(_check_text(c))})
    return out


def build_prompt(case: dict, verdict: dict, sources: list[dict]) -> str:
    numbered = "\n\n".join(f"[{i + 1}] {s['source']}: {s['text']}" for i, s in enumerate(sources))
    return (f"Case: {redact_pii(case.get('subject'))}\n"
            f"Aggregator: {', '.join(case.get('third_parties') or []) or 'not stated'}\n"
            f"Salesforce waiting on: {case.get('waiting_raw') or 'not stated'}\n"
            f"Whose turn (rule-based): {verdict.get('ball') or 'unknown'}\n\n"
            f"Sources:\n{numbered or 'none'}")


_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").lower()).strip()


def parse(raw: str, sources: list[dict]) -> dict[str, Any]:
    """The model's answer, with every evidence line checked against the source it cites.

    A line is `verified` only if its quote really appears in that source. One that cites a source that
    doesn't exist, or quotes words that aren't there, moves to not_verified so it isn't read as proof."""
    body = json.loads(raw)
    evidence, moved = [], []
    for e in body.get("evidence") or []:
        claim, quote = str(e.get("claim") or "").strip(), str(e.get("quote") or "").strip()
        try:
            n = int(e.get("source"))
        except (TypeError, ValueError):
            n = 0
        if not claim:
            continue
        if not 1 <= n <= len(sources):
            moved.append(f"{claim} (no valid source)")
            continue
        src = sources[n - 1]
        if not quote or _norm(quote) not in _norm(src["text"]):
            moved.append(f"{claim} (the quoted words aren't in source {n})")
            continue
        evidence.append({"claim": claim, "source": n, "source_name": src["source"],
                         "url": src.get("url"), "quote": quote})
    return {"root_cause": str(body.get("root_cause") or "Not established"),
            "evidence": evidence,
            "not_verified": [str(x) for x in body.get("not_verified") or []] + moved,
            "next_action": str(body.get("next_action") or ""),
            "draft_reply": str(body.get("draft_reply") or ""),
            "sources": [{"n": i + 1, "source": s["source"], "url": s.get("url")}
                        for i, s in enumerate(sources)]}


def to_rows(result: dict) -> list[dict]:
    """A write-up as rows for monitor_case_checks, one per section item."""
    rows = [{"section": "evidence", "claim": e["claim"], "source": e.get("source_name") or e.get("source"),
             "quote": e["quote"], "url": e.get("url")} for e in result.get("evidence") or []]
    rows += [{"section": "not_verified", "claim": x} for x in result.get("not_verified") or []]
    if result.get("next_action"):
        rows.append({"section": "next_action", "claim": result["next_action"]})
    if result.get("draft_reply"):
        rows.append({"section": "draft_reply", "claim": result["draft_reply"]})
    return rows


def from_check(check: dict) -> dict[str, Any]:
    """The write-up held in a saved check's rows, for the case page."""
    rows = check.get("rows") or []
    pick = lambda s: [r for r in rows if isinstance(r, dict) and r.get("section") == s]  # noqa: E731
    one = lambda s: next((r.get("claim") for r in pick(s)), "")  # noqa: E731
    return {"root_cause": check.get("summary") or "Not established",
            "evidence": [{"claim": r.get("claim"), "source_name": r.get("source"),
                          "quote": r.get("quote"), "url": r.get("url")} for r in pick("evidence")],
            "not_verified": [r.get("claim") for r in pick("not_verified")],
            "next_action": one("next_action"), "draft_reply": one("draft_reply"),
            "saved_by": check.get("saved_by"), "created_at": check.get("created_at")}
