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


def polish(guidance: str, case: dict, *, model: str | None = None) -> str:
    body = f"Subject: {case.get('subject', '')}\n\n{case.get('body', '')}".strip()
    out = llm.complete(
        system=_SYSTEM,
        user=f"# Customer's case\n{body}\n\n# Agent's answer (authoritative)\n{guidance}",
        model=model or llm.DEFAULT_MODEL,
        max_tokens=600,
    )
    return (out or guidance).strip()


def resume_from_guidance(case: dict, guidance: str, *, cfg=None,
                         tenant_id: str | None = None) -> dict:
    """Polish `guidance` into a reply and deliver it on `case`'s channel.
    Returns {sent, via, auto_sent, reply}. Never raises."""
    try:
        reply = polish(guidance, case)
    except Exception as e:  # noqa: BLE001
        log.warning("polish failed (%s); sending the guidance verbatim", e)
        reply = guidance.strip()

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
            if sent and case_id and salesforce.available():
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
