"""Phase 25 — image attachments (OCR), sf_context, the ai_prompt node."""

from __future__ import annotations

import json

import pytest

from interpreter import attachments, llm, sf_context
from interpreter.registry import h_ai_prompt, h_attachments, h_sf_context


# ── llm vision plumbing ────────────────────────────────────────────
def test_vision_chain_is_free_first(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    chain = llm._vision_chain("claude-haiku-4-5")
    assert chain[-1] == "claude-haiku-4-5"                 # paid last
    assert all(":free" in m for m in chain[:-1])           # free first


def test_complete_with_images_stubs_when_no_vision_model(monkeypatch):
    for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    out = llm.complete(system="s", user="describe", images=[(b"\x89PNG", "image/png")])
    assert isinstance(out, str) and out                    # deterministic stub, no crash


# ── attachments / OCR ─────────────────────────────────────────────
def test_ocr_bytes_no_engine_is_empty(monkeypatch):
    monkeypatch.setattr(attachments, "_ocr_engine", lambda: None)
    assert attachments.ocr_bytes(b"whatever") == ""


class _SF:
    base_url = "https://x/services/data/v60.0/"
    session_id = "tok"

    class session:
        @staticmethod
        def get(url, **kw):
            return type("R", (), {"content": b"IMGBYTES", "raise_for_status": lambda self: None})()

    def query(self, soql):
        if "ContentDocumentLink" in soql:
            return {"records": [{"ContentDocumentId": "069AAA"}]}
        if "ContentVersion" in soql:
            return {"records": [{"Id": "068AAA", "Title": "error", "FileExtension": "png",
                                 "FileType": "PNG", "ContentSize": 12}]}
        return {"records": []}


def test_extract_pulls_images_and_ocr(monkeypatch):
    from interpreter import salesforce
    monkeypatch.setattr(salesforce, "available", lambda: True)
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _SF())
    monkeypatch.setattr(attachments, "ocr_bytes", lambda data: "ORA-01017: invalid password")

    out = attachments.extract({"sf_id": "500X"}, tenant_id="t", skip_signatures=False)
    assert len(out["attachments"]) == 1
    a = out["attachments"][0]
    assert a["filename"] == "error.png" and a["mime"] == "image/png"
    assert a["ocr_text"] == "ORA-01017: invalid password"
    assert out["attachment_text"].endswith("ORA-01017: invalid password")
    assert out["_blobs"][a["blob_key"]] == b"IMGBYTES"


def test_h_attachments_writes_state(monkeypatch):
    monkeypatch.setattr(attachments, "extract",
                        lambda *a, **k: {"attachments": [{"filename": "a.png", "kind": "image",
                                                          "blob_key": "068"}],
                                         "attachment_text": "hello", "_blobs": {"068": b"x"}})
    out = h_attachments({"case": {"sf_id": "500X"}, "tenant_id": "t"}, {"_node_id": "n"})
    assert out["attachment_text"] == "hello"
    assert out["_attachment_blobs"] == {"068": b"x"}
    assert out["trace"][0]["type"] == "attachments"


# ── video ─────────────────────────────────────────────────────────
def test_transcribe_no_whisper_is_empty(monkeypatch):
    monkeypatch.setattr(attachments, "_whisper_model", lambda: None)
    assert attachments.transcribe(b"\x00\x00moov") == ""


def test_video_frames_no_ffmpeg_is_empty(monkeypatch):
    monkeypatch.setattr(attachments, "_ffmpeg", lambda: None)
    assert attachments.video_frames(b"data") == []


def test_process_video_combines_transcript_and_frame_ocr(monkeypatch):
    monkeypatch.setattr(attachments, "transcribe", lambda d, **k: "so I click submit and it 500s")
    monkeypatch.setattr(attachments, "video_frames", lambda d, **k: [b"F1", b"F2"])
    monkeypatch.setattr(attachments, "ocr_bytes",
                        lambda fr: "HTTP 500 Internal Server Error" if fr == b"F2" else "")
    v = attachments.process_video(b"vid", "repro.mp4")
    assert v["transcript"].startswith("so I click")
    assert "HTTP 500" in v["frame_ocr"] and v["frames"] == [b"F1", b"F2"]


class _VidSF(_SF):
    def query(self, soql):
        if "ContentVersion" in soql:
            return {"records": [{"Id": "068VID", "Title": "repro", "FileExtension": "mp4",
                                 "FileType": "MP4", "ContentSize": 2048}]}
        return super().query(soql)


def test_extract_processes_video_when_opted_in(monkeypatch):
    from interpreter import salesforce
    monkeypatch.setattr(salesforce, "available", lambda: True)
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _VidSF())
    monkeypatch.setattr(attachments, "process_video",
                        lambda data, name, **k: {"transcript": "the button does nothing",
                                                 "frame_ocr": "Save failed", "frames": [b"K0", b"K1"]})

    out = attachments.extract({"sf_id": "500X"}, tenant_id="t", do_video=True)
    a = out["attachments"][0]
    assert a["kind"] == "video" and a["transcript"] == "the button does nothing"
    assert "narration: the button does nothing" in out["attachment_text"]
    assert "on-screen text: Save failed" in out["attachment_text"]
    assert set(out["_blobs"].values()) == {b"K0", b"K1"}          # keyframes for vision


def test_extract_skips_video_by_default(monkeypatch):
    from interpreter import salesforce
    monkeypatch.setattr(salesforce, "available", lambda: True)
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _VidSF())
    monkeypatch.setattr(attachments, "process_video",
                        lambda *a, **k: pytest.fail("video processing must be opt-in"))
    out = attachments.extract({"sf_id": "500X"}, tenant_id="t")   # do_video defaults False
    assert out["attachments"] == []


# ── sf_context ────────────────────────────────────────────────────
class _CtxSF:
    def query(self, soql):
        if "AccountTeamMember" in soql:
            return {"records": []}
        if "FROM Account " in soql and "COUNT" not in soql:
            return {"records": [{"Id": "001A", "Name": "Northwind", "Type": "Customer",
                                 "Industry": "Retail", "OwnerId": "005O", "Owner": {"Name": "Priya"},
                                 "ParentId": None, "Parent": None,
                                 "Customer_Type__c": "enterprise", "Region__c": "EMEA"}]}
        if "COUNT(Id) c FROM Account" in soql:
            return {"records": [{"c": 2}]}
        if "FROM Contact WHERE Id" in soql:
            return {"records": [{"Id": "003C", "Name": "Sam", "Email": "s@nw.com", "Title": "Ops"}]}
        if "FROM Contact WHERE AccountId" in soql:
            return {"records": [{"Name": "Jo", "Email": "jo@nw.com", "Title": "Admin"}]}
        if "FROM Case WHERE AccountId" in soql and "COUNT" not in soql:
            return {"records": [{"CaseNumber": "001", "Subject": "prev", "Status": "Closed",
                                 "IsClosed": True, "CreatedDate": "2026-01-01"}]}
        if "COUNT(Id) c FROM Case" in soql and "IsClosed = false" in soql:
            return {"records": [{"c": 1}]}
        if "COUNT(Id) c FROM Case" in soql:
            return {"records": [{"c": 9}]}
        return {"records": []}


def test_sf_context_load(monkeypatch):
    from interpreter import salesforce
    monkeypatch.setattr(salesforce, "available", lambda: True)
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _CtxSF())

    ctx = sf_context.load({"account_id": "001A", "contact_id": "003C"},
                          want={"account", "contacts", "cases", "team"}, tenant_id="t")
    assert ctx["account"]["tier"] == "enterprise" and ctx["account"]["owner_user"] == "Priya"
    assert ctx["contact"]["name"] == "Sam"
    assert ctx["siblings"][0]["name"] == "Jo"
    assert ctx["cases"] == {"total": 9, "open": 1, "recent": ctx["cases"]["recent"]}
    # AccountTeamMember empty -> falls back to the account owner
    assert ctx["account_team"][0]["name"] == "Priya"


def test_h_sf_context_node(monkeypatch):
    monkeypatch.setattr(sf_context, "load", lambda *a, **k: {"account": {"name": "NW"},
                                                             "cases": {"open": 2, "total": 5}})
    out = h_sf_context({"sender": {"account_id": "001A"}, "tenant_id": "t"}, {"_node_id": "n"})
    assert out["sf_context"]["account"]["name"] == "NW"
    assert "2 open / 5" in out["trace"][0]["summary"]


# ── ai_prompt node ───────────────────────────────────────────────
def test_ai_prompt_interpolates_and_writes_output(monkeypatch):
    seen = {}
    monkeypatch.setattr(llm, "complete",
                        lambda **kw: seen.update(kw) or "the answer")
    state = {"case": {"subject": "Login broken", "body": "cannot sign in"},
             "sf_context": {"account": {"name": "Northwind", "tier": "enterprise"}},
             "attachment_text": "ORA-01017"}
    cfg = {"_node_id": "n", "output_key": "triage_notes",
           "system": "triage", "user": "Case {case.subject} for {sf_context.account.name} "
                     "(tier {sf_context.account.tier})\nimg: {attachment_text}"}
    out = h_ai_prompt(state, cfg)
    assert out["ai"]["triage_notes"] == "the answer"
    assert "Login broken for Northwind (tier enterprise)" in seen["user"]
    assert "ORA-01017" in seen["user"]


def test_ai_prompt_json_schema_parses(monkeypatch):
    monkeypatch.setattr(llm, "complete", lambda **kw: '{"intent": "billing", "severity": "high"}')
    out = h_ai_prompt({"case": {"subject": "charge", "body": "x"}},
                      {"_node_id": "n", "output_key": "triage", "user": "{case.subject}",
                       "json_schema": {"type": "object"}})
    assert out["ai"]["triage"] == {"intent": "billing", "severity": "high"}


def test_ai_prompt_images_auto_pulls_blobs(monkeypatch):
    got = {}
    monkeypatch.setattr(llm, "complete", lambda **kw: got.update(kw) or "seen it")
    state = {"case": {"subject": "s", "body": "b"},
             "attachments": [{"blob_key": "068", "mime": "image/jpeg"}],
             "_attachment_blobs": {"068": b"JPEGDATA"}}
    h_ai_prompt(state, {"_node_id": "n", "user": "{case.subject}", "images": "auto"})
    assert got["images"] == [(b"JPEGDATA", "image/jpeg")]
    assert got["cache"] is False                            # never cache a vision call


def test_ai_prompt_on_error_passthrough(monkeypatch):
    def boom(**kw):
        raise RuntimeError("model down")
    monkeypatch.setattr(llm, "complete", boom)
    out = h_ai_prompt({"case": {"subject": "s", "body": "b"}},
                      {"_node_id": "n", "output_key": "x", "user": "{case.subject}",
                       "on_error": "passthrough"})
    assert out["ai"]["x"] is None
    with pytest.raises(RuntimeError):
        h_ai_prompt({"case": {"subject": "s", "body": "b"}},
                    {"_node_id": "n", "user": "{case.subject}", "on_error": "fail"})
