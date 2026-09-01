"""
Phase 24b — the Slack reasoning dialogue.

No case gets an AI answer from automation. After triage the bot tags the
responsible agent; when the agent hands the case to the bot in Slack, the bot
runs a *reasoning conversation*: it works through a bank of 4–6 "pointer
questions" (seed bank per Case.Type + an LLM top-up), proposing its own read
of each and letting the agent confirm / correct / add. It works through
**every** pointer — it never short-circuits to a draft once it has "enough".
Only when all pointers are covered does it compose the customer-facing reply,
and it sends only after the agent explicitly approves.

    awaiting_handoff ─(agent: "take")→ reasoning ─(all pointers answered)→
        drafting → awaiting_approval ─(agent: "send")→ sent
                                     └(agent: "no")───→ abandoned

`advance()` is pure given an `llm_fn` — the DB lives in `handle_agent_message`.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from typing import Any, Callable

from interpreter import llm

log = logging.getLogger("interpreter.reasoning")

STATES = ("awaiting_handoff", "reasoning", "drafting", "awaiting_approval",
          "sent", "abandoned")

_HANDOFF = {
    "take", "take it", "take this", "take the case", "you take it", "over to you",
    "go", "go ahead", "start", "begin", "reason", "reason it", "let's reason",
    "lets reason", "work it", "work through it", "handoff", "hand off", "yours",
    "bot take this", "help", "help me", "your turn",
}
_ABANDON = {"no", "not yet", "cancel", "hold", "stop", "abandon", "leave it",
            "i'll handle it", "ill handle it", "nvm", "never mind"}
_APPROVE_EXACT = {"looks good", "send it", "sounds good", "go for it", "ship it",
                  "good to go", "that works", "perfect", "all good"}
_APPROVE_WORDS = {"send", "approve", "approved", "lgtm", "ship", "approve.",
                  "confirmed"}
_EDIT_PREFIX = ("edit", "change", "reword", "tweak", "revise", "shorten",
                "no,", "not quite", "almost")


def _is_approve(text: str) -> bool:
    t = (text or "").strip().strip(".!👍✅🚀 ").lower()
    if t in _APPROVE_EXACT or t in {"yes", "yep", "yeah", "ok", "okay", "sure",
                                    "confirm", "confirmed", "👍", "✅"}:
        return True
    return bool(set(re.findall(r"[a-z']+", t)) & _APPROVE_WORDS)

_MAX_POINTERS = 6
_MIN_POINTERS = 4


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _default_llm(system: str, user: str, *, max_tokens: int = 400,
                 model: str | None = None) -> str:
    try:
        return (llm.complete(system=system, user=user,
                             model=model or llm.DEFAULT_MODEL,
                             max_tokens=max_tokens) or "").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("reasoning llm call failed: %s", e)
        return ""


LLMFn = Callable[..., str]


# ── pointer bank ─────────────────────────────────────────────────────
_FALLBACK_POINTERS = [
    "What is the customer really asking for, in one sentence?",
    "What do we know for certain vs. what are we assuming?",
    "Does a good answer need the customer's own data, or is a general answer enough?",
    "What is the risk if we answer wrong?",
]


def seed_pointers(sb, case_type: str | None) -> list[str]:
    """The per-Case.Type seed bank (migration 056's `pointer_bank`)."""
    ct = (case_type or "Other").strip()
    try:
        rows = sb.table("pointer_bank").select("pointers").eq("case_type", ct).execute().data
        if not rows:
            rows = sb.table("pointer_bank").select("pointers").eq("case_type", "Other").execute().data
        if rows and rows[0].get("pointers"):
            got = rows[0]["pointers"]
            return list(got) if isinstance(got, list) else list(_FALLBACK_POINTERS)
    except Exception as e:  # noqa: BLE001
        log.warning("pointer_bank read failed (%s); using fallback", e)
    return list(_FALLBACK_POINTERS)


_TOPUP_SYS = (
    "You add 1–2 SHORT, case-specific diagnostic questions a support agent and "
    "an AI should reason through before answering this customer — things the "
    "generic checklist below misses for THIS case. Return a JSON array of "
    "strings, 0 to 2 items, no prose. Do not repeat the generic questions."
)


def build_pointers(sb, *, case_type: str | None, case: dict,
                   kb_hits: list | None = None, llm_fn: LLMFn | None = None) -> list[dict]:
    """Seed bank + an LLM top-up, capped at `_MAX_POINTERS`. Each pointer is
    `{"q", "answered": False, "bot_take": None, "agent_note": None}`."""
    llm_fn = llm_fn or _default_llm
    seed = seed_pointers(sb, case_type)
    extra: list[str] = []
    if len(seed) < _MAX_POINTERS:
        body = (f"Case type: {case_type or 'Other'}\n"
                f"Subject: {case.get('subject', '')}\n"
                f"Body: {(case.get('body') or '')[:1500]}\n\n"
                f"Generic questions already covered:\n- " + "\n- ".join(seed))
        raw = llm_fn(_TOPUP_SYS, body, max_tokens=250)
        try:
            got = json.loads(raw[raw.find("["): raw.rfind("]") + 1] or "[]")
            extra = [str(x).strip() for x in got if str(x).strip()][:2]
        except Exception:  # noqa: BLE001
            extra = []
    qs = (seed + extra)[:_MAX_POINTERS]
    if len(qs) < _MIN_POINTERS:
        qs += _FALLBACK_POINTERS[: _MIN_POINTERS - len(qs)]
    return [{"q": q, "answered": False, "bot_take": None, "agent_note": None} for q in qs]


# ── session construction ────────────────────────────────────────────
def open_session(sb, *, case: dict, tenant_id: str, run_id: str | None = None,
                 case_type: str | None = None, case_number: str | None = None,
                 agent_sf_id: str | None = None, agent_slack_id: str | None = None,
                 kb_hits: list | None = None, llm_fn: LLMFn | None = None) -> dict:
    """Create (or return the existing open) reasoning session for a Case."""
    case_id = case.get("sf_id") or case.get("id")
    existing = (sb.table("reasoning_sessions").select("*")
                .eq("case_id", case_id)
                .not_.in_("state", ("sent", "abandoned")).execute().data)
    if existing:
        return existing[0]
    pointers = build_pointers(sb, case_type=case_type, case=case,
                              kb_hits=kb_hits, llm_fn=llm_fn)
    row = {
        "tenant_id": tenant_id, "case_id": case_id, "run_id": run_id,
        "case_number": case_number or case.get("case_number"),
        "state": "awaiting_handoff", "agent_sf_id": agent_sf_id,
        "agent_slack_id": agent_slack_id, "pointers": pointers, "cursor": 0,
        "transcript": [],
    }
    return sb.table("reasoning_sessions").insert(row).execute().data[0]


# ── the dialogue engine (pure) ──────────────────────────────────────
def is_handoff(text: str) -> bool:
    t = (text or "").strip().strip("!.?").lower().lstrip("@")
    if t in _HANDOFF:
        return True
    return any(t.startswith(w + " ") or t == w for w in _HANDOFF) or "take this" in t


_TAKE_SYS = (
    "You are a senior support engineer reasoning WITH a colleague about a "
    "customer case — not answering the customer. For the one question below, "
    "give your best current read in 1–3 sentences, grounded ONLY in the case "
    "text and the notes provided. If answering it properly would need the "
    "customer's own data that we don't have, say so plainly. No hedging boilerplate."
)

_DRAFT_SYS = (
    "Write the customer-facing reply, using ONLY the case and the agreed "
    "reasoning notes below. Concise, friendly, plain text, no preamble. If the "
    "notes say the real answer needs the customer's specific data we don't "
    "have, the reply must ask for that / set expectations — do NOT invent "
    "specifics or present a generic scenario as if it were their situation."
)


def _issue_summary(case: dict) -> str:
    b = (case.get("body") or "").strip().replace("\n", " ")
    return (b[:300] + "…") if len(b) > 300 else b


def _bot_take(pointer_q: str, case: dict, pointers: list[dict],
              kb_hits: list | None, llm_fn: LLMFn) -> str:
    prior = "\n".join(f"- {p['q']}\n  agreed: {p['agent_note']}"
                      for p in pointers if p.get("answered") and p.get("agent_note"))
    kb = "\n".join(f"- {h}" for h in (kb_hits or [])[:5])
    user = (f"Case: {case.get('subject', '')}\n{_issue_summary(case)}\n\n"
            f"{'Notes so far:\n' + prior + '\n\n' if prior else ''}"
            f"{'KB:\n' + kb + '\n\n' if kb else ''}"
            f"Question: {pointer_q}")
    return llm_fn(_TAKE_SYS, user, max_tokens=220) or "(no read — over to you)"


def _compose_draft(case: dict, pointers: list[dict], kb_hits: list | None,
                   llm_fn: LLMFn, extra_instruction: str = "") -> str:
    notes = "\n".join(f"- {p['q']}\n  → {p.get('agent_note') or p.get('bot_take') or ''}"
                      for p in pointers)
    kb = "\n".join(f"- {h}" for h in (kb_hits or [])[:6])
    user = (f"Customer case\nSubject: {case.get('subject', '')}\n{_issue_summary(case)}\n\n"
            f"Agreed reasoning notes\n{notes}\n"
            f"{('\nKB\n' + kb) if kb else ''}"
            f"{('\n\nAlso apply: ' + extra_instruction) if extra_instruction else ''}")
    return llm_fn(_DRAFT_SYS, user, max_tokens=650) or ""


def _n_answered(pointers: list[dict]) -> int:
    return sum(1 for p in pointers if p.get("answered"))


def _first_unanswered(pointers: list[dict]) -> int:
    for i, p in enumerate(pointers):
        if not p.get("answered"):
            return i
    return len(pointers)


def _pointer_block(i: int, n: int, p: dict) -> str:
    return f"*{i + 1}/{n}. {p['q']}*\nMy read: {p['bot_take']}"


def advance(session: dict, text: str, *, case: dict, kb_hits: list | None = None,
            llm_fn: LLMFn | None = None) -> dict[str, Any]:
    """Advance the dialogue by one agent message. Returns
    `{"reply": str, "session": dict, "action": None | "send" | "abandoned"}`.
    Pure — no DB, no Slack. `session` is returned mutated."""
    llm_fn = llm_fn or _default_llm
    state = session.get("state", "awaiting_handoff")
    pointers: list[dict] = session.get("pointers") or []
    n = len(pointers)
    session.setdefault("transcript", []).append(
        {"role": "agent", "text": text, "at": _now()})

    def done(reply: str, action: str | None = None) -> dict[str, Any]:
        session["transcript"].append({"role": "bot", "text": reply, "at": _now()})
        session["updated_at"] = _now()
        return {"reply": reply, "session": session, "action": action}

    if state in ("sent", "abandoned"):
        return done(f"This case's dialogue is already _{state}_. Start a new "
                    f"one from Salesforce if you need to.")

    if state == "awaiting_handoff":
        if not is_handoff(text):
            return done("When you want to reason this one through together, "
                        "reply `take` (or @mention me).")
        i = 0
        pointers[i]["bot_take"] = _bot_take(pointers[i]["q"], case, pointers, kb_hits, llm_fn)
        session["state"] = "reasoning"
        session["cursor"] = i
        opener = (
            f"*Case {session.get('case_number') or session.get('case_id')}* — "
            f"{case.get('subject', '(no subject)')}\n{_issue_summary(case)}\n\n"
            f"Let's reason through this — {n} points, and I'll want your read on "
            f"every one before I draft anything.\n\n{_pointer_block(i, n, pointers[i])}\n\n"
            f"Confirm, correct me, or add detail.")
        return done(opener)

    if state == "reasoning":
        cur = session.get("cursor", _first_unanswered(pointers))
        if 0 <= cur < n:
            pointers[cur]["agent_note"] = text.strip()
            pointers[cur]["answered"] = True
        nxt = _first_unanswered(pointers)
        if nxt < n:
            pointers[nxt]["bot_take"] = _bot_take(pointers[nxt]["q"], case, pointers, kb_hits, llm_fn)
            session["cursor"] = nxt
            return done(f"Noted.\n\n{_pointer_block(nxt, n, pointers[nxt])}")
        # every pointer covered -> draft
        session["state"] = "drafting"
        draft = _compose_draft(case, pointers, kb_hits, llm_fn)
        session["draft"] = draft
        session["state"] = "awaiting_approval"
        return done(
            f"That's all {n} points. Here's my draft to the customer:\n\n"
            f"————\n{draft}\n————\n\n"
            f"Reply `send` to send it, `edit: <what to change>`, or `no` to hold.")

    if state == "awaiting_approval":
        low = text.strip().lower()
        if low in _ABANDON:
            session["state"] = "abandoned"
            return done("Holding — the Case stays with you.", action="abandoned")
        if low.startswith(_EDIT_PREFIX) or "edit:" in low:
            instr = text.split(":", 1)[1].strip() if ":" in text else text
            draft = _compose_draft(case, pointers, kb_hits, llm_fn, extra_instruction=instr)
            session["draft"] = draft
            return done(f"Updated draft:\n\n————\n{draft}\n————\n\n"
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
                         llm_fn: LLMFn | None = None) -> dict[str, Any]:
    """Load the case, advance the dialogue, persist the session. Returns the
    same shape as `advance()`. The caller (slackbot) posts `reply` and, on
    `action == 'send'`, delivers `session['draft']`."""
    case = _case_for_session(sb, session_row)
    out = advance(session_row, text, case=case, llm_fn=llm_fn)
    s = out["session"]
    try:
        sb.table("reasoning_sessions").update({
            "state": s["state"], "pointers": s["pointers"], "cursor": s.get("cursor", 0),
            "draft": s.get("draft"), "transcript": s.get("transcript", []),
            "updated_at": "now()",
        }).eq("session_id", s["session_id"]).execute()
    except Exception as e:  # noqa: BLE001
        log.warning("reasoning_sessions persist failed for %s: %s",
                    s.get("session_id"), e)
    return out
