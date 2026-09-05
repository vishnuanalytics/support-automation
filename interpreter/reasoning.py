"""
Phase 24 — the Slack reasoning dialogue between the bot and the responsible agent.

No case gets an AI answer from automation. After triage the bot tags the agent;
when the agent hands the case to the bot in Slack (an @mention, or `take`), the
bot:

  1. picks the questions that actually matter for THIS case (LLM prunes a
     per-Case.Type seed bank — a basic case may need 1–2, not 6),
  2. asks them **all in one message**, each with the bot's own tentative read,
  3. lets the agent answer free-form; if a *critical* point is still open it
     asks a short follow-up — at most `max_rounds` (default 3) rounds total,
  4. drafts the customer reply and sends it **only** on explicit approval.

    awaiting_handoff ─(@mention / "take")→ clarifying ─(no critical gap
        | max rounds)→ drafting → awaiting_approval ─("send")→ sent
                                                    └("no")──→ abandoned

`advance()` is pure given an `llm_fn`; the DB lives in `handle_agent_message`.
`cursor` on the row is reused as the clarification-round counter.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
from typing import Any, Callable

from interpreter import llm

log = logging.getLogger("interpreter.reasoning")

STATES = ("awaiting_handoff", "clarifying", "drafting", "awaiting_approval",
          "sent", "abandoned")

DEFAULT_MAX_ROUNDS = int(os.environ.get("REASONING_MAX_ROUNDS", "3") or 3)
_MAX_QUESTIONS = 5

# @mentioning the bot in the thread also counts as a handoff.
_HANDOFF = {
    "take", "take it", "take this", "take the case", "you take it", "over to you",
    "go", "go ahead", "start", "begin", "reason", "reason it", "let's reason",
    "lets reason", "work it", "work through it", "handoff", "hand off", "yours",
    "bot take this", "help", "help me", "your turn",
    "assist", "engage", "pick up", "pickup", "let's go", "lets go", "ready",
    "walk me through", "walk through", "discuss", "let's discuss", "lets discuss",
} | {w.strip().lower() for w in os.environ.get("HANDOFF_WORDS", "").split(",") if w.strip()}
_ABANDON = {"no", "not yet", "cancel", "hold", "stop", "abandon", "leave it",
            "i'll handle it", "ill handle it", "nvm", "never mind"}
_APPROVE_EXACT = {"looks good", "send it", "sounds good", "go for it", "ship it",
                  "good to go", "that works", "perfect", "all good"}
_APPROVE_WORDS = {"send", "approve", "approved", "lgtm", "ship", "confirmed"}
_EDIT_PREFIX = ("edit", "change", "reword", "tweak", "revise", "shorten",
                "no,", "not quite", "almost")

LLMFn = Callable[..., str]


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _default_llm(system: str, user: str, *, max_tokens: int = 500,
                 model: str | None = None) -> str:
    try:
        return (llm.complete(system=system, user=user,
                             model=model or llm.DEFAULT_MODEL,
                             max_tokens=max_tokens) or "").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("reasoning llm call failed: %s", e)
        return ""


def is_handoff(text: str) -> bool:
    t = (text or "").strip().strip("!.?").lower().lstrip("@")
    if t in _HANDOFF:
        return True
    return any(t.startswith(w + " ") or t == w for w in _HANDOFF) or "take this" in t


def _is_approve(text: str) -> bool:
    t = (text or "").strip().strip(".!👍✅🚀 ").lower()
    if t in _APPROVE_EXACT or t in {"yes", "yep", "yeah", "ok", "okay", "sure",
                                    "confirm", "confirmed", "👍", "✅"}:
        return True
    return bool(set(re.findall(r"[a-z']+", t)) & _APPROVE_WORDS)


def _json_slice(raw: str, open_ch: str = "[", close_ch: str = "]"):
    try:
        return json.loads(raw[raw.find(open_ch): raw.rfind(close_ch) + 1] or "null")
    except Exception:  # noqa: BLE001
        return None


def _issue_summary(case: dict) -> str:
    b = (case.get("body") or case.get("description") or "").strip().replace("\n", " ")
    return (b[:400] + "…") if len(b) > 400 else b


# ── question planning ───────────────────────────────────────────────
_FALLBACK_Q = [
    {"q": "What is the customer really asking for, in one sentence?", "critical": True},
    {"q": "What do we know for certain vs. what are we assuming?", "critical": True},
    {"q": "Does a good answer need the customer's own data, or is a general answer enough?",
     "critical": False},
]


def seed_pointers(sb, case_type: str | None) -> list[str]:
    ct = (case_type or "Other").strip()
    try:
        rows = sb.table("pointer_bank").select("pointers").eq("case_type", ct).execute().data
        if not rows:
            rows = sb.table("pointer_bank").select("pointers").eq("case_type", "Other").execute().data
        if rows and isinstance(rows[0].get("pointers"), list):
            return list(rows[0]["pointers"])
    except Exception as e:  # noqa: BLE001
        log.warning("pointer_bank read failed (%s)", e)
    return [p["q"] for p in _FALLBACK_Q]


_PLAN_SYS = (
    "You are triaging a customer support case with a colleague before drafting a "
    "reply. From the candidate questions below, keep ONLY the ones that genuinely "
    "need an answer for THIS case — a simple/known case may need just 1 or 2. You "
    "may reword for concision and add at most ONE case-specific question the list "
    "misses. Mark `critical` true only if we cannot safely reply without it. "
    "Return a JSON array (max 5) of {\"q\": string, \"critical\": boolean}. JSON only."
)


def plan_questions(sb, *, case_type: str | None, case: dict,
                   kb_hits: list | None = None, llm_fn: LLMFn | None = None) -> list[dict]:
    """LLM-pruned subset of the seed bank for this case. 1–5 questions."""
    llm_fn = llm_fn or _default_llm
    seed = seed_pointers(sb, case_type)
    user = (f"Case type: {case_type or 'Other'}\n"
            f"Subject: {case.get('subject', '')}\n"
            f"Body: {_issue_summary(case)}\n"
            f"{('KB hits: ' + '; '.join(str(h) for h in kb_hits[:4])) if kb_hits else ''}\n\n"
            f"Candidate questions:\n- " + "\n- ".join(seed))
    got = _json_slice(llm_fn(_PLAN_SYS, user, max_tokens=400))
    picked: list[dict] = []
    if isinstance(got, list):
        for item in got[:_MAX_QUESTIONS]:
            q = str((item or {}).get("q") or "").strip() if isinstance(item, dict) else str(item).strip()
            if q:
                picked.append({"q": q, "critical": bool(isinstance(item, dict) and item.get("critical"))})
    if not picked:
        picked = [dict(p) for p in _FALLBACK_Q]
    for p in picked:
        p.update(answered=False, agent_note=None)
    return picked


# back-compat alias (older callers / tests)
build_pointers = plan_questions


# ── message builders (LLM) ─────────────────────────────────────────
_ASK_SYS = (
    "You are a senior support engineer briefing a colleague on a customer case "
    "before you draft the reply together. For EACH numbered question, give your "
    "current best read in ONE sentence, grounded ONLY in the case + KB. If a "
    "question needs the customer's own data we don't have, say so. Output ONLY a "
    "numbered list, one item per question, format:\n"
    "`N. <question>`\n`   _my read:_ <one sentence>`"
)

_INGEST_SYS = (
    "A colleague replied (free-form) to a set of questions about a support case. "
    "For EACH question in order, decide if the reply now answers it, and capture "
    "their answer in one short line. Return a JSON array, same length and order as "
    "the questions, of {\"answered\": boolean, \"note\": string}. JSON only."
)

_DRAFT_SYS = (
    "Write the customer-facing reply, using ONLY the case and the agreed reasoning "
    "notes below. Concise, friendly, plain text, no preamble. Where a note says we "
    "need the customer's specific data we don't have, the reply must ask for it / "
    "set expectations — do NOT invent specifics or present a generic scenario as "
    "if it were their situation."
)


def _kb_block(kb_hits: list | None, n: int = 5) -> str:
    return "\n".join(f"- {h}" for h in (kb_hits or [])[:n])


def _ask_all(pointers: list[dict], case: dict, kb_hits: list | None, llm_fn: LLMFn) -> str:
    qs = "\n".join(f"{i + 1}. {p['q']}" for i, p in enumerate(pointers))
    user = (f"Case: {case.get('subject', '')}\n{_issue_summary(case)}\n\n"
            f"{('KB:\n' + _kb_block(kb_hits) + '\n\n') if kb_hits else ''}"
            f"Questions:\n{qs}")
    body = llm_fn(_ASK_SYS, user, max_tokens=600)
    if not body:
        body = "\n".join(f"{i + 1}. {p['q']}\n   _my read:_ (over to you)"
                         for i, p in enumerate(pointers))
    return body


def _ingest(pointers: list[dict], agent_text: str, llm_fn: LLMFn) -> None:
    qs = "\n".join(f"{i + 1}. {p['q']}" for i, p in enumerate(pointers))
    got = _json_slice(llm_fn(_INGEST_SYS, f"Questions:\n{qs}\n\nColleague's reply:\n{agent_text}",
                             max_tokens=500))
    if isinstance(got, list):
        for p, item in zip(pointers, got):
            if isinstance(item, dict) and item.get("answered"):
                p["answered"] = True
                p["agent_note"] = str(item.get("note") or "").strip() or p.get("agent_note")
    # fallback: a substantive reply marks the non-critical ones covered
    if not any(p["answered"] for p in pointers) and len(agent_text.split()) >= 4:
        for p in pointers:
            if not p["critical"]:
                p["answered"] = True
                p["agent_note"] = agent_text.strip()[:300]


# ── Phase 29 step 5 — autonomous continuation of a stalled dialogue ───
# `reasoning_ttl` (interpreter/sweeps.py) used to have exactly two moves for
# a `clarifying` session the human agent stopped replying to: nudge once,
# then escalate + abandon — the reasoning already done (the questions were
# picked, the thread exists) was thrown away every time. This gives the bot
# ONE bounded, genuinely agentic shot at closing the still-open CRITICAL
# pointers itself before giving up to a human queue — the exact gap the
# Phase 29 kickoff note flagged ("the LLM never picks its own next
# action"). Reuses complete_with_tools (step 1) + hybrid_retrieve exactly
# like h_agent (step 2/registry.py) rather than reimplementing a ReAct
# loop — the model decides whether a KB search would help or whether to
# give up, it isn't a canned retry.
_AUTONOMOUS_TOOLS = [
    {
        "name": "search_kb",
        "description": ("Search the knowledge base for documentation that answers "
                         "one of the still-open questions."),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "the search query"}},
            "required": ["query"],
        },
    },
    {
        "name": "give_up",
        "description": ("Stop -- these questions need the human agent's own judgement "
                         "or the customer's own account data, not something documented."),
        "parameters": {"type": "object", "properties": {}},
    },
]

_AUTONOMOUS_SYS = (
    "A colleague went quiet mid-conversation about a support case. Before "
    "escalating, see if documentation alone can close the open questions below "
    "-- call search_kb with a specific query, or give_up if these genuinely "
    "need a human's judgement or the customer's own account data rather than "
    "something written down."
)

_AUTONOMOUS_INGEST_SYS = (
    "You searched documentation to answer open questions about a support case "
    "because the human agent went quiet. For EACH question, decide if the "
    "documentation below actually answers it -- do NOT guess or use general "
    "knowledge, only what's written. Return a JSON array, same length and "
    "order as the questions, of {\"answered\": boolean, \"note\": string}. "
    "JSON only."
)


def _autonomous_search(query: str, tenant_id: str | None) -> list[dict]:
    from .retrieval import hybrid_retrieve

    try:
        results, _score = hybrid_retrieve(query, top_k=4, tenant_id=tenant_id)
    except Exception as e:  # noqa: BLE001
        log.warning("reasoning autonomous_continue search failed: %s", e)
        return []
    return [{"doc_url": r.get("doc_url"), "chunk_text": r.get("chunk_text") or ""}
            for r in results]


def autonomous_continue(session: dict, case: dict, *, tenant_id: str | None = None,
                        model: str | None = None, max_iterations: int = 3,
                        llm_fn: LLMFn | None = None) -> dict[str, Any]:
    """One bounded attempt for the bot to close out a stalled `clarifying`
    session's still-open CRITICAL pointers itself, instead of only nudging
    then abandoning to a human queue. Pure like `advance()`: mutates and
    returns `pointers` (the caller persists). Grounded-only — a pointer is
    marked answered here only off documentation the search actually
    returned, never an ungrounded guess, so the stub path (no tools ever
    called) never resolves anything, deterministically. **Never sends
    anything itself** — the caller still needs a human `send` before a
    resulting draft reaches the customer, same as every other draft this
    module produces; this only unsticks the *dialogue*, not the approval
    gate (KIL/step-4's "flag to a human, never act silently" applies here
    too). Returns `{"pointers", "resolved", "iterations", "kb_hits"}` —
    `resolved` is True only when every critical pointer is now answered.
    """
    llm_fn = llm_fn or _default_llm
    pointers = _norm(session.get("pointers") or [])
    gaps = _open_gaps(pointers, critical_only=True)
    if not gaps:
        return {"pointers": pointers, "resolved": True, "iterations": 0, "kb_hits": []}

    tenant_id = tenant_id or session.get("tenant_id")
    _model = model or llm.FAST_MODEL
    qs = "\n".join(f"{i + 1}. {p['q']}" for i, p in enumerate(gaps))
    messages: list[dict[str, Any]] = [{"role": "user", "content":
        f"Case: {case.get('subject', '')}\n{_issue_summary(case)}\n\n"
        f"Open questions the human agent hasn't answered:\n{qs}"}]
    found: list[dict] = []
    iterations = 0
    for _ in range(max(1, int(max_iterations))):
        try:
            result = llm.complete_with_tools(
                messages=messages, system=_AUTONOMOUS_SYS, tools=_AUTONOMOUS_TOOLS,
                model=_model, max_tokens=300, tenant_id=tenant_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("reasoning autonomous_continue call failed: %s", e)
            break
        calls = [tc for tc in result.tool_calls if tc.name == "search_kb"]
        if not calls:
            break
        iterations += 1
        messages.append({"role": "assistant", "content": result.text,
                         "tool_calls": [{"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                                        for tc in result.tool_calls]})
        for tc in calls:
            q = str(tc.arguments.get("query") or "").strip()
            if not q:
                continue
            hits = _autonomous_search(q, tenant_id)
            found.extend(hits)
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": "\n".join(h["chunk_text"] for h in hits) or "(no results)"})

    if not found:
        return {"pointers": pointers, "resolved": False, "iterations": iterations, "kb_hits": []}

    docs = "\n".join(f"- {h['chunk_text'][:500]}" for h in found[:8] if h.get("chunk_text"))
    got = _json_slice(llm_fn(_AUTONOMOUS_INGEST_SYS, f"Questions:\n{qs}\n\nDocumentation found:\n{docs}",
                             max_tokens=500))
    if isinstance(got, list):
        for p, item in zip(gaps, got):
            if isinstance(item, dict) and item.get("answered"):
                note = str(item.get("note") or "").strip()
                p["answered"] = True
                p["agent_note"] = f"[autonomous, unconfirmed by human] {note}"[:300]
    resolved = not _open_gaps(pointers, critical_only=True)
    kb_hits = [h["chunk_text"][:300] for h in found if h.get("chunk_text")]
    return {"pointers": pointers, "resolved": resolved, "iterations": iterations, "kb_hits": kb_hits}


def _norm(pointers: list[dict]) -> list[dict]:
    """Tolerate pre-24e rows (no `critical` key)."""
    for p in pointers or []:
        p.setdefault("critical", False)
        p.setdefault("answered", False)
        p.setdefault("agent_note", None)
    return pointers or []


def _open_gaps(pointers: list[dict], critical_only: bool = True) -> list[dict]:
    return [p for p in pointers
            if not p.get("answered") and (p.get("critical") or not critical_only)]


def _compose_draft(case: dict, pointers: list[dict], kb_hits: list | None,
                   llm_fn: LLMFn, extra_instruction: str = "") -> str:
    notes = "\n".join(
        f"- {p['q']}\n  → {p.get('agent_note') or '(agent did not confirm — use judgement / ask the customer)'}"
        for p in pointers)
    user = (f"Customer case\nSubject: {case.get('subject', '')}\n{_issue_summary(case)}\n\n"
            f"Agreed reasoning notes\n{notes}\n"
            f"{('\nKB\n' + _kb_block(kb_hits, 6)) if kb_hits else ''}"
            f"{('\n\nAlso apply: ' + extra_instruction) if extra_instruction else ''}")
    return llm_fn(_DRAFT_SYS, user, max_tokens=650) or ""


# ── session construction ────────────────────────────────────────────
def open_session(sb, *, case: dict, tenant_id: str, run_id: str | None = None,
                 case_type: str | None = None, case_number: str | None = None,
                 agent_sf_id: str | None = None, agent_slack_id: str | None = None,
                 kb_hits: list | None = None, max_rounds: int | None = None,
                 llm_fn: LLMFn | None = None) -> dict:
    """Create (or return the existing open) reasoning session for a Case."""
    case_id = case.get("sf_id") or case.get("id")
    existing = (sb.table("reasoning_sessions").select("*")
                .eq("case_id", case_id)
                .not_.in_("state", ("sent", "abandoned")).execute().data)
    if existing:
        return existing[0]
    pointers = plan_questions(sb, case_type=case_type, case=case,
                              kb_hits=kb_hits, llm_fn=llm_fn)
    row = {
        "tenant_id": tenant_id, "case_id": case_id, "run_id": run_id,
        "case_number": case_number or case.get("case_number"),
        "state": "awaiting_handoff", "agent_sf_id": agent_sf_id,
        "agent_slack_id": agent_slack_id, "pointers": pointers, "cursor": 0,
        "max_rounds": int(max_rounds or DEFAULT_MAX_ROUNDS), "transcript": [],
    }
    return sb.table("reasoning_sessions").insert(row).execute().data[0]


# ── the dialogue engine (pure) ─────────────────────────────────────
def advance(session: dict, text: str, *, case: dict, kb_hits: list | None = None,
            llm_fn: LLMFn | None = None, handoff: bool | None = None) -> dict[str, Any]:
    """Advance the dialogue by one agent message. Returns
    `{"reply", "session", "action": None | "send" | "abandoned"}`. Pure — no DB,
    no Slack. `session` is returned mutated. `handoff` (the agent @mentioned the
    bot) overrides the keyword check in `awaiting_handoff`."""
    llm_fn = llm_fn or _default_llm
    state = session.get("state", "awaiting_handoff")
    pointers: list[dict] = _norm(session.get("pointers") or [])
    max_rounds = int(session.get("max_rounds") or DEFAULT_MAX_ROUNDS)
    session.setdefault("transcript", []).append({"role": "agent", "text": text, "at": _now()})

    def done(reply: str, action: str | None = None) -> dict[str, Any]:
        session["transcript"].append({"role": "bot", "text": reply, "at": _now()})
        session["updated_at"] = _now()
        return {"reply": reply, "session": session, "action": action}

    if state in ("sent", "abandoned"):
        return done(f"This case's dialogue is already _{state}_.")

    if state == "awaiting_handoff":
        if not (handoff or is_handoff(text)):
            return done("Reply *@support automation* in this thread — or type "
                        "`take` — when you want to reason through this one together.")
        session["state"] = "clarifying"
        session["cursor"] = 1                       # round 1
        hdr = (f"*Case {session.get('case_number') or session.get('case_id')}* — "
               f"{case.get('subject', '(no subject)')}\n{_issue_summary(case)}\n\n"
               f"Here's my read on the {'point' if len(pointers) == 1 else 'points'} "
               f"that matter — correct anything wrong and fill any gaps (one reply "
               f"is fine):\n\n")
        return done(hdr + _ask_all(pointers, case, kb_hits, llm_fn))

    if state == "clarifying":
        rnd = int(session.get("cursor") or 1)
        _ingest(pointers, text.strip(), llm_fn)
        gaps = _open_gaps(pointers, critical_only=True)
        if gaps and rnd < max_rounds:
            session["cursor"] = rnd + 1
            bullets = "\n".join(f"• {p['q']}" for p in gaps)
            return done(f"Thanks. Still need your read on "
                        f"{'this' if len(gaps) == 1 else 'these'} before I draft "
                        f"(round {rnd + 1}/{max_rounds}):\n\n{bullets}")
        # enough — or we've used our rounds
        draft = _compose_draft(case, pointers, kb_hits, llm_fn)
        session["draft"] = draft
        session["state"] = "awaiting_approval"
        tail = ("" if not gaps else
                f"\n\n_(still unconfirmed: {', '.join(p['q'] for p in gaps)} — "
                f"I've drafted conservatively.)_")
        return done(f"Here's my draft to the customer:\n\n————\n{draft}\n————{tail}\n\n"
                    f"Reply `send`, `edit: <what to change>`, or `no` to hold.")

    if state == "awaiting_approval":
        low = text.strip().lower()
        if low in _ABANDON:
            session["state"] = "abandoned"
            return done("Holding — the Case stays with you.", action="abandoned")
        if low.startswith(_EDIT_PREFIX) or "edit:" in low:
            instr = text.split(":", 1)[1].strip() if ":" in text else text
            session["draft"] = _compose_draft(case, pointers, kb_hits, llm_fn,
                                              extra_instruction=instr)
            return done(f"Updated draft:\n\n————\n{session['draft']}\n————\n\n"
                        f"`send`, `edit: <more>`, or `no`.")
        if _is_approve(text):
            session["state"] = "sent"
            return done("Sending now. ✅", action="send")
        return done("Reply `send`, `edit: <changes>`, or `no`.")

    return done("(unrecognised state — start again from Salesforce.)")


# ── DB-facing wrapper ──────────────────────────────────────────────
def _case_for_session(sb, session: dict) -> dict:
    if session.get("run_id"):
        try:
            r = (sb.table("runs").select("case_payload")
                 .eq("run_id", session["run_id"]).execute().data)
            if r and r[0].get("case_payload"):
                return r[0]["case_payload"]
        except Exception as e:  # noqa: BLE001
            log.warning("run lookup for session failed: %s", e)
    from interpreter import salesforce
    try:
        return salesforce.get_case(session["case_id"])
    except Exception:  # noqa: BLE001
        return {"sf_id": session["case_id"], "subject": session.get("case_number") or ""}


def handle_agent_message(sb, session_row: dict, text: str, *,
                         llm_fn: LLMFn | None = None,
                         handoff: bool | None = None) -> dict[str, Any]:
    """Load the case, advance the dialogue, persist the session."""
    case = _case_for_session(sb, session_row)
    out = advance(session_row, text, case=case, llm_fn=llm_fn, handoff=handoff)
    s = out["session"]
    try:
        sb.table("reasoning_sessions").update({
            "state": s["state"], "pointers": s["pointers"], "cursor": s.get("cursor", 0),
            "draft": s.get("draft"), "transcript": s.get("transcript", []),
            "updated_at": "now()",
        }).eq("session_id", s["session_id"]).execute()
    except Exception as e:  # noqa: BLE001
        log.warning("reasoning_sessions persist failed for %s: %s", s.get("session_id"), e)
    return out
