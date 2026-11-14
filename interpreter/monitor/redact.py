"""Text hygiene for anything stored, shown, or sent to an LLM."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_TEXT = 400

_URL = re.compile(r"https?://[^\s<>\"'|]+")
_SIGNED = re.compile(r"^(signature|awsaccesskeyid|x-amz-.*|expires)$", re.I)

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{7,}\d(?![\w])")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
_IFSC = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
_CARD_OR_ACCOUNT = re.compile(r"\b\d(?:[ -]?\d){8,18}\b")


def strip_signed_url(url: str) -> str:
    """Drop pre-signed credential params, keeping the rest of the URL."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _SIGNED.match(k)]
    return urlunsplit(parts._replace(query=urlencode(kept)))


def strip_signed_urls(text: str) -> str:
    return _URL.sub(lambda m: strip_signed_url(m.group(0)), text or "")


def clean(text: str | None, limit: int = MAX_TEXT) -> str:
    """Strip signed params, then truncate to the stored-text limit."""
    out = strip_signed_urls(text or "").strip()
    return out if len(out) <= limit else out[:limit].rstrip() + "…"


def redact_pii(text: str | None) -> str:
    """Remove emails, phones and payment/bank details before any LLM call."""
    out = _EMAIL.sub("[email]", text or "")
    out = _IBAN.sub("[bank]", out)
    out = _IFSC.sub("[bank]", out)
    out = _PHONE.sub("[phone]", out)
    return _CARD_OR_ACCOUNT.sub("[number]", out)
