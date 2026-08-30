"""
Phase 20c -- outbound side of the email channel + the hard guard.

`decide(outcome, cfg, clarification)` is pure: given the flow's final
outcome and the channel config it returns one of
``send_reply`` / ``send_questions`` / ``needs_human`` / ``noop``. The
worker (not a flow node -- the graph stays channel-agnostic) calls it
after an email-sourced run and acts:

  * a customer-facing email goes out **only** on ``outcome.action ==
    "auto_reply"`` and only when the channel's ``auto_send_enabled`` master
    switch is on;
  * clarifying questions go out only when the flow said ``need_info`` *and*
    the `clarify` node opted in (`clarification.auto_send`) *and* the
    master switch is on;
  * ``ask_human`` / ``handover`` (and a disabled switch, an empty draft, …)
    -> the message is flagged unread for a human, nothing is sent.

`send_reply` builds an RFC-822 reply (threaded via In-Reply-To/References,
stamped ``X-Support-Bot: 1`` so the poller never answers it) and sends it
over SMTP (imap provider) or the Gmail API (gmail provider). No creds ->
dry-run. Never raises.
"""

from __future__ import annotations

import logging

log = logging.getLogger("interpreter.emailer")

_BOT_HEADER = ("X-Support-Bot", "1")


def decide(outcome: dict | None, cfg, clarification: dict | None) -> tuple[str, dict]:
    """Pure. -> (action, payload). action in
    {send_reply, send_questions, needs_human, noop}."""
    o = outcome or {}
    action = o.get("action")

    if action == "auto_reply":
        if not getattr(cfg, "auto_send_enabled", False):
            return "needs_human", {"reason": "auto-send is off for this channel"}
        reply = (o.get("reply") or "").strip()
        if not reply:
            return "needs_human", {"reason": "auto_reply with an empty draft"}
        return "send_reply", {"body": reply}

    if action == "need_info":
        questions = [q for q in (o.get("questions") or []) if str(q).strip()]
        opted_in = bool((clarification or {}).get("auto_send"))
        if getattr(cfg, "auto_send_enabled", False) and opted_in and questions:
            return "send_questions", {"questions": questions}
        return "needs_human", {"reason": "clarify not auto-sent"}

    if action in ("ask_human", "handover"):
        return "needs_human", {"reason": action}

    return "noop", {"reason": f"action={action!r}"}


def _subject_reply(subject: str) -> str:
    s = (subject or "").strip() or "your request"
    return s if s[:3].lower() == "re:" else f"Re: {s}"


def _questions_body(questions: list[str]) -> str:
    numbered = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
    return ("Thanks for reaching out. To help you with this, could you share:\n\n"
            + numbered + "\n\nJust reply to this email and we'll follow up.")


def send_reply(cfg, *, to: str, subject: str, body: str,
               in_reply_to: str = "", references=None, dry_run: bool = False) -> dict:
    """Send one reply. Returns {sent, dry_run, to, via, message_id, error}.
    Never raises."""
    from email.message import EmailMessage
    from email.utils import formatdate, make_msgid

    references = references or []
    result: dict = {"sent": False, "dry_run": False, "to": to,
                    "via": "smtp" if cfg.provider != "gmail" else "gmail",
                    "message_id": None, "error": None}
    if not (to or "").strip() or not (body or "").strip():
        result.update(dry_run=True, error="missing recipient or body")
        return result

    msg = EmailMessage()
    from_name = (cfg.from_name or "").strip()
    msg["From"] = f"{from_name} <{cfg.send_from}>" if from_name else cfg.send_from
    msg["To"] = to
    msg["Subject"] = _subject_reply(subject)
    msg["Date"] = formatdate(localtime=True)
    mid = make_msgid(domain=(cfg.send_from.split("@", 1) or [""])[-1] or None)
    msg["Message-ID"] = mid
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join([*references, in_reply_to][-10:])
    msg[_BOT_HEADER[0]] = _BOT_HEADER[1]
    msg["Auto-Submitted"] = "auto-replied"
    msg.set_content(body)
    result["message_id"] = mid

    has_creds = (cfg.provider == "gmail" and cfg.secret.get("refresh_token")) or (
        cfg.provider != "gmail" and cfg.smtp_host and cfg.secret.get("password"))
    if dry_run or not has_creds:
        result["dry_run"] = True
        if not dry_run:
            result["error"] = "no send credentials — dry run"
        log.info("[email dry-run] would send to %s: %s", to, msg["Subject"])
        return result

    try:
        if cfg.provider == "gmail":
            _send_gmail(cfg, msg)
        else:
            _send_smtp(cfg, msg)
        result["sent"] = True
    except Exception as e:  # noqa: BLE001
        result["error"] = str(e)
        log.warning("email send to %s failed: %s", to, e)
    return result


def _send_smtp(cfg, msg) -> None:
    import smtplib

    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
        s.starttls()
        s.login(cfg.username, cfg.secret.get("password", ""))
        s.send_message(msg)


def _send_gmail(cfg, msg) -> None:
    import base64

    from interpreter.mailbox import _gmail_service

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    _gmail_service(cfg).users().messages().send(userId="me", body={"raw": raw}).execute()
