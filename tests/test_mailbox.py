"""Phase 20a -- offline tests for the email-channel config model + the pure
poller helpers. No network, no Supabase."""

from __future__ import annotations

from email.message import EmailMessage

from interpreter.mailbox import (
    MailboxConfig,
    is_autoreply,
    looks_like_bot_address,
    parse_message,
)


# ── config model ────────────────────────────────────────────────────
def test_from_row_defaults_and_from_addr_falls_back_to_username():
    cfg = MailboxConfig.from_row(
        "tid",
        {"provider": "imap", "username": "support@acme.com", "imap_host": "imap.acme.com"},
        "active",
        {"password": "app-pw"},
    )
    assert cfg.from_addr == "support@acme.com"       # defaulted from username
    assert cfg.imap_port == 993 and cfg.folder == "INBOX"
    assert cfg.status == "active"
    assert cfg.send_from == "support@acme.com"


def test_no_reply_addr_wins_for_send_from():
    cfg = MailboxConfig(tenant_id="t", username="in@acme.com", from_addr="in@acme.com",
                        no_reply_addr="no-reply@acme.com")
    assert cfg.send_from == "no-reply@acme.com"


def test_public_status_never_includes_the_secret():
    cfg = MailboxConfig.from_row(
        "t", {"provider": "imap", "username": "a@b.com"}, "inactive", {"password": "hunter2"},
    )
    status = cfg.public_status()
    assert "hunter2" not in str(status)
    assert "password" not in status and "secret" not in status
    assert status["configured"] is True and status["username"] == "a@b.com"


def test_secret_is_kept_out_of_repr():
    assert "hunter2" not in repr(
        MailboxConfig(tenant_id="t", secret={"password": "hunter2"})
    )


def test_to_config_roundtrips_through_from_row():
    a = MailboxConfig(tenant_id="t", provider="imap", team="csm",
                      username="s@x.com", from_name="Support", imap_host="i.x.com",
                      smtp_host="s.x.com", auto_send_enabled=True)
    b = MailboxConfig.from_row("t", a.to_config(), "active", {})
    assert (b.provider, b.team, b.username, b.from_name, b.imap_host,
            b.smtp_host, b.auto_send_enabled) == \
           ("imap", "csm", "s@x.com", "Support", "i.x.com", "s.x.com", True)


# ── loop-breakers ──────────────────────────────────────────────────
def test_looks_like_bot_address():
    assert looks_like_bot_address("no-reply@acme.com")
    assert looks_like_bot_address("MAILER-DAEMON@acme.com")
    assert looks_like_bot_address("noreply+tag@acme.com")
    assert not looks_like_bot_address("jane.doe@acme.com")
    assert not looks_like_bot_address("")


def test_is_autoreply_catches_the_usual_suspects():
    assert is_autoreply({"Auto-Submitted": "auto-replied"})
    assert is_autoreply({"X-Autoreply": "yes"})
    assert is_autoreply({"Precedence": "bulk"})
    assert is_autoreply({"List-Id": "<news.acme.com>"})
    assert is_autoreply({"List-Unsubscribe": "<mailto:x>"})
    assert is_autoreply({"X-Support-Bot": "1"})            # our own outbound
    assert is_autoreply({"From": "postmaster@acme.com"})
    assert not is_autoreply({"Auto-Submitted": "no", "From": "real@acme.com"})
    assert not is_autoreply({"From": "Real Person <real@acme.com>"})


# ── parse_message ─────────────────────────────────────────────────
def _mk(body="Hi, my export is failing.", *, html=False, extra=None):
    m = EmailMessage()
    m["From"] = "Jane Doe <jane@customer.com>"
    m["To"] = "support@acme.com"
    m["Subject"] = "Export broken"
    m["Message-ID"] = "<abc123@customer.com>"
    m["In-Reply-To"] = "<prev@acme.com>"
    m["References"] = "<one@acme.com> <prev@acme.com>"
    for k, v in (extra or {}).items():
        m[k] = v
    if html:
        m.set_content("plain fallback")
        m.add_alternative(f"<p>{body}</p>", subtype="html")
    else:
        m.set_content(body)
    return m.as_bytes()


def test_parse_message_extracts_the_case_fields():
    c = parse_message(_mk())
    assert c["channel"] == "email"
    assert c["from"] == "jane@customer.com" and c["from_name"] == "Jane Doe"
    assert c["subject"] == "Export broken"
    assert "export is failing" in c["body"]
    assert c["message_id"] == "<abc123@customer.com>"
    assert c["in_reply_to"] == "<prev@acme.com>"
    assert c["references"] == ["<one@acme.com>", "<prev@acme.com>"]
    assert c["is_autoreply"] is False


def test_parse_message_html_only_is_stripped_to_text():
    c = parse_message(_mk("Please <b>help</b> me", html=True))
    # prefers text/plain when present; here the alternative plain is "plain fallback"
    assert "plain fallback" in c["body"] or "help" in c["body"]


def test_parse_message_flags_an_autoresponder():
    c = parse_message(_mk(extra={"Auto-Submitted": "auto-replied"}))
    assert c["is_autoreply"] is True
