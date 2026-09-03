"""
Resume after a human: an agent left internal guidance (a CaseComment) on a
Case the bot had escalated -> turn that guidance into a polished,
customer-facing reply and send it.

Unlike a customer reply (which re-runs the whole flow -- fresh triage), an
agent's answer *is* the source of truth, so this is a single LLM polish
step, then the same delivery path `auto_reply` uses. The channel's
`auto_send_enabled` master switch still applies: off -> the polished reply
is left on the Case as a draft CaseComment for the agent to send.

Called from `api.worker._check_resolution`. Never raises.
"""

from __future__ import annotations

import logging

from interpreter import llm, salesforce

log = logging.getLogger("interpreter.agent_reply")

_SYSTEM = (
    "An agent has provided the authoritative answer to a customer's support "
    "case. Rewrite it as a concise, friendly, customer-facing reply. Use ONLY "
    "the agent's answer for facts -- do not add information. No preamble like "
    "'Here is the reply'. Return plain text."
)

_SYSTEM_WITH_DRAFT = (
    "The support bot drafted a reply to a customer. An agent reviewed it and "
    "left a short note. Produce the FINAL customer-facing reply: start from the "
    "bot's draft and apply the agent's note. If the note is just approval "
    "('send it', 'send this', 'lgtm', 'approved'), return the draft essentially "
    "unchanged (only fix obvious typos). Use ONLY the draft and the note for "
    "facts -- add nothing. No preamble. Return plain text."
)

# a bare approval -> just send the bot's own draft
_APPROVAL = {
    "send it", "send this", "send this response to customer", "send the reply",
    "send the draft", "send", "sent", "lgtm", "looks good", "approved", "approve",
    "ok", "okay", "yes", "go ahead", "ship it", "send to customer",
    "send this response", "send response", "send the response to customer",
}


def _is_approval(note: str) -> bool:
    n = (note or "").strip().strip(".!").lower()
    return n in _APPROVAL or (len(n) <= 40 and n.startswith("send ") and "customer" in n)


def polish(guidance: str, case: dict, *, draft: str | None = None,
           model: str | None = None) -> str:
    body = f"Subject: {case.get('subject', '')}\n\n{case.get('body', '')}".strip()
    if draft and draft.strip():
        if _is_approval(guidance):
            return draft.strip()
        out = llm.complete(
            system=_SYSTEM_WITH_DRAFT,
            user=(f"# Customer's case\n{body}\n\n# Bot's draft reply\n{draft.strip()}"
                  f"\n\n# Agent's note\n{guidance}"),
            model=model or llm.DEFAULT_MODEL,
            max_tokens=700,
        )
        return (out or draft).strip()
    out = llm.complete(
        system=_SYSTEM,
        user=f"# Customer's case\n{body}\n\n# Agent's answer (authoritative)\n{guidance}",
        model=model or llm.DEFAULT_MODEL,
        max_tokens=600,
    )
    return (out or guidance).strip()


def resume_from_guidance(case: dict, guidance: str, *, cfg=None,
                         tenant_id: str | None = None, draft: str | None = None) -> dict:
    """Polish `guidance` into a reply and deliver it on `case`'s channel.
    When `draft` is given (the bot's original reply), the guidance is applied
    *on top of* it — a bare "send it" just sends the draft.
    Returns {sent, via, auto_sent, reply}. Never raises."""
    try:
        reply = polish(guidance, case, draft=draft)
    except Exception as e:  # noqa: BLE001
        log.warning("polish failed (%s); falling back", e)
        reply = (draft or guidance).strip()

    case_id = case.get("sf_id") or case.get("id")
    to_email = (case.get("from") or (case.get("contact") or {}).get("email")
                or case.get("supplied_email") or "")
    channel = case.get("channel") or "salesforce"
    auto_send = bool(getattr(cfg, "auto_send_enabled", False)) if cfg is not None else True

    if not auto_send:
        # leave it for the agent to review/send
        res = salesforce.add_case_comment(
            case_id, f"[bot draft — auto-send is off]\n\n{reply}", tenant_id=tenant_id)
        return {"sent": False, "auto_sent": False, "via": "case_comment_draft",
                "reply": reply, "detail": res}

    try:
        if channel == "email" and cfg is not None and to_email:
            from interpreter import emailer

            r = emailer.send_reply(cfg, to=to_email,
                                   subject=case.get("subject") or "your request",
                                   body=reply,
                                   in_reply_to=case.get("message_id") or "",
                                   references=case.get("references") or [])
            sent = bool(r.get("sent"))
            # log_email_message() already resolves this tenant's own creds
            # internally (client_for, not the env-only available()) -- no
            # need to gate on available() here too, which used to skip this
            # entirely for a self-serve tenant with no env creds even
            # though the call underneath would have worked fine.
            if sent and case_id:
                try:
                    salesforce.log_email_message(
                        case_id, incoming=False, status=salesforce._EM_SENT,
                        from_addr=cfg.send_from, from_name=cfg.from_name or "",
                        to_addrs=to_email, subject=emailer._subject_reply(case.get("subject") or ""),
                        body=reply, message_id=r.get("message_id") or "", tenant_id=tenant_id)
                except Exception as e:  # noqa: BLE001
                    log.warning("mirror agent reply to Case failed: %s", e)
            return {"sent": sent, "auto_sent": sent, "via": "smtp", "reply": reply, "detail": r}

        r = salesforce.send_case_reply(case_id, reply, to_email=to_email or None,
                                       subject=case.get("subject") or "your request",
                                       tenant_id=tenant_id)
        return {"sent": bool(r.get("sent")), "auto_sent": bool(r.get("sent")),
                "via": r.get("via"), "reply": reply, "detail": r}
    except Exception as e:  # noqa: BLE001
        log.warning("resume_from_guidance delivery failed: %s", e)
        return {"sent": False, "auto_sent": False, "via": "error", "reply": reply, "error": str(e)}
