"""Phase 26 — live free-model roster + signature/logo filtering."""

from __future__ import annotations

import pytest

from interpreter import attachments, llm, roster
from scripts import refresh_llm_roster as rr


# ── roster ranking ────────────────────────────────────────────────
def _m(id, prompt="0", completion="0", ctx=100000, inp=None, out=None):
    return {"id": id, "context_length": ctx,
            "pricing": {"prompt": prompt, "completion": completion},
            "architecture": {"input_modalities": inp or ["text"],
                             "output_modalities": out or ["text"]}}


CATALOG = [
    _m("google/gemma-9/free-ish:free", ctx=1_000_000, inp=["text", "image"]),
    _m("meta-llama/llama-x-70b-instruct:free", ctx=128000),
    _m("nvidia/nemotron-huge-340b:free", ctx=64000),
    _m("cohere/rerank-3:free"),                                    # not chat -> skip
    _m("google/lyria-music:free", out=["audio"]),                  # not text out -> skip
    _m("openrouter/auto"),                                         # meta -> skip
    _m("z-ai/glm-9:free", inp=["text", "image", "video"]),
    _m("google/gemini-flash-lite", prompt="0.00000005", completion="0.0000002",
       inp=["text", "image"]),                                     # cheap paid
    _m("openai/gpt-4o-mini", prompt="0.00000015", completion="0.0000006",
       inp=["text", "image"]),
    _m("anthropic/claude-x", prompt="0.000003", completion="0.000015"),  # pricier paid
    _m("some/model:batch", prompt="0", completion="0"),            # :batch -> skip
]


def test_build_ranks_free_first_and_drops_junk():
    plan = rr.build(CATALOG)
    text_free = plan["text"]["models"]
    assert "google/gemma-9/free-ish:free" == text_free[0]          # google vendor rank 0
    assert "cohere/rerank-3:free" not in text_free
    assert "google/lyria-music:free" not in text_free
    assert "openrouter/auto" not in text_free
    assert "some/model:batch" not in text_free
    # premium tail = cheapest capable paid, google/openai before anthropic
    assert plan["text"]["premium"][0] in ("google/gemini-flash-lite", "openai/gpt-4o-mini")


def test_build_vision_needs_image_input():
    v = rr.build(CATALOG)["vision"]["models"]
    assert "google/gemma-9/free-ish:free" in v          # has image
    assert "meta-llama/llama-x-70b-instruct:free" not in v   # text only
    assert "z-ai/glm-9:free" in v


def test_build_video_needs_explicit_video_modality():
    vid = rr.build(CATALOG)["video"]["models"]
    assert vid == ["z-ai/glm-9:free"]                    # only one lists video


# ── llm.py reads the roster ──────────────────────────────────────
def test_fallback_chain_uses_the_roster(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setattr(roster, "chain",
                        lambda cap: (["free/a:free", "free/b:free"], ["paid/c"])
                        if cap == "text" else ([], []))
    chain = llm._fallback_chain("openai/gpt-oss-120b")
    assert chain[:3] == ["openai/gpt-oss-120b", "free/a:free", "free/b:free"]
    assert chain[-1] == "paid/c"


def test_vision_chain_uses_the_roster(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    for k in ("ANTHROPIC_API_KEY",):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(roster, "chain",
                        lambda cap: (["v/free:free"], ["v/paid"]) if cap == "vision" else ([], []))
    chain = llm._vision_chain()
    assert "v/free:free" in chain and chain.index("v/free:free") < chain.index("v/paid")


# ── signature / logo filter ─────────────────────────────────────
def _png(w, h):
    import io
    import os

    from PIL import Image
    img = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))   # noise -> realistic size
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def test_looks_like_signature():
    big = _png(1200, 800)                       # a real screenshot
    assert attachments.looks_like_signature("screenshot.png", big) is False
    assert attachments.looks_like_signature("image001.png", big) is True       # inline-sig name
    assert attachments.looks_like_signature("company-logo.png", big) is True
    assert attachments.looks_like_signature("linkedin.png", big) is True
    assert attachments.looks_like_signature("x.png", b"tiny") is True          # < min bytes
    assert attachments.looks_like_signature("art.png", _png(3000, 450)) is True   # 6.7:1 strip
    assert attachments.looks_like_signature("thumb.png", _png(90, 90)) is True      # < 350 px


class _SigSB:
    def __init__(self, seen=0):
        self.seen = seen
        self.upserts = []

    def table(self, _n):
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def upsert(self, row, **_k):
        self.upserts.append(row)
        return self

    def execute(self):
        return type("R", (), {"data": [{"seen": self.seen}] if self.seen else []})


def test_seen_signature_threshold():
    sb = _SigSB(seen=0)
    assert attachments._seen_signature(sb, "t", "acme.com", b"IMG", 2) is False   # 1st
    sb2 = _SigSB(seen=2)
    assert attachments._seen_signature(sb2, "t", "acme.com", b"IMG", 2) is True   # 3rd -> skip
    assert sb2.upserts[-1]["seen"] == 3


def test_extract_skips_a_logo_keeps_a_screenshot(monkeypatch):
    from interpreter import salesforce
    monkeypatch.setattr(salesforce, "available", lambda: True)

    logo, shot = _png(80, 80), _png(1200, 800)

    class _SF:
        base_url = "u/"
        session_id = "t"

        class session:
            calls = {"n": 0}

            @classmethod
            def get(cls, url, **kw):
                cls.calls["n"] += 1
                data = logo if cls.calls["n"] == 1 else shot
                return type("R", (), {"content": data, "raise_for_status": lambda s: None})()

        def query(self, soql):
            if "ContentDocumentLink" in soql:
                return {"records": [{"ContentDocumentId": "069A"}, {"ContentDocumentId": "069B"}]}
            if "ContentVersion" in soql:
                return {"records": [
                    {"Id": "0681", "Title": "image001", "FileExtension": "png", "ContentSize": len(logo)},
                    {"Id": "0682", "Title": "shot", "FileExtension": "png", "ContentSize": len(shot)}]}
            return {"records": []}

    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _SF())
    monkeypatch.setattr(attachments, "ocr_bytes", lambda d: "READABLE" if d == shot else "x")

    out = attachments.extract({"sf_id": "500X", "from": "joe@acme.com"}, tenant_id="t", sb=None)
    kinds = {a["filename"]: a for a in out["attachments"]}
    assert kinds["image001.png"].get("skipped") == "signature/logo"
    assert "ocr_text" not in kinds["image001.png"]
    assert kinds["shot.png"]["ocr_text"] == "READABLE"
    assert "READABLE" in out["attachment_text"]
