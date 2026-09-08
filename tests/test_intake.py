"""interpreter/intake.py — checklist matching, detect rules, extraction,
gaps / questions / field-writes. Offline: `llm.complete` is stubbed and the
Supabase client is a fluent fake."""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import intake


# --------------------------------------------------------------------------- #
# a fluent stand-in for `sb.table(...).select(...).eq(...).eq(...).execute()`
# --------------------------------------------------------------------------- #
class _Q:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


class _SB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return _Q(self._rows)


IMG_CHECKLIST = {
    "label": "Menu images not visible",
    "priority": 20,
    "match": {"keywords": ["image", "not visible"]},
    "signals": [
        {"key": "channels", "label": "Channels", "question": "Which channels?",
         "required": True, "detect": {"any_of": ["swiggy", "zomato"]},
         "lands_in": {"ctx": "affected_channels"}},
        {"key": "outlet_ref", "label": "Outlet", "question": "Which outlet ID?",
         "required": True, "detect": {"regex": r"outlet\s*[:#]?\s*[A-Za-z0-9-]{3,}"},
         "lands_in": {"sf_field": "SubModule__c"}},
        {"key": "last_publish", "label": "Publish status", "question": "Publish status?",
         "required": True, "lands_in": {"ctx": "last_publish_status"}},
        {"key": "error_text", "label": "Error text", "question": "Any error text?",
         "required": False, "lands_in": {"ctx": "publish_error_text"}},
        {"key": "screenshot", "label": "Screenshot", "question": "Attach a screenshot?",
         "required": True, "detect": {"attachment_type": "image"},
         "lands_in": {"ctx": "has_screenshot"}, "vision": True},
    ],
}

SYNC_CHECKLIST = {
    "label": "Price out of sync",
    "priority": 5,
    "match": {"keywords": ["price"], "case_type": "Problem / Bug"},
    "signals": [
        {"key": "channels", "label": "Channels", "question": "Which channels?",
         "required": True, "lands_in": {"ctx": "affected_channels"}},
    ],
}


def _state(**over):
    st = {
        "tenant_id": "T1",
        "case": {"sf_id": "500X", "subject": "Menu image issue",
                 "body": "images are not visible on swiggy and zomato"},
        "classification": {"topic": "menu-images", "case_type": "Problem / Bug"},
        "attachments": [],
    }
    st.update(over)
    return st


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #
def test_checklist_for_keyword_match():
    sb = _SB([IMG_CHECKLIST])
    got = intake.checklist_for(_state(), sb=sb)
    assert got and got["label"] == "Menu images not visible"


def test_checklist_for_no_match_returns_none():
    sb = _SB([IMG_CHECKLIST])
    st = _state(case={"subject": "billing question", "body": "how do I pay my invoice"},
                classification={"topic": "billing"})
    assert intake.checklist_for(st, sb=sb) is None


def test_checklist_for_most_specific_wins_over_priority():
    # SYNC has lower priority (5 < 20) but matches 2 conditions (keyword + case_type)
    # vs IMG's 1 — the more specific match wins.
    sb = _SB([IMG_CHECKLIST, SYNC_CHECKLIST])
    st = _state(case={"subject": "price wrong", "body": "old price and image not visible"},
                classification={"topic": "x", "case_type": "Problem / Bug"})
    got = intake.checklist_for(st, sb=sb)
    assert got["label"] == "Price out of sync"


def test_checklist_for_case_type_mismatch_excludes():
    sb = _SB([SYNC_CHECKLIST])
    st = _state(case={"subject": "price wrong", "body": "old price"},
                classification={"topic": "x", "case_type": "Question"})
    assert intake.checklist_for(st, sb=sb) is None


def test_load_checklists_swallows_errors():
    class _Boom:
        def table(self, *_a):
            raise RuntimeError("db down")

    assert intake.load_checklists("T1", sb=_Boom()) == []


# --------------------------------------------------------------------------- #
# detect rules
# --------------------------------------------------------------------------- #
def test_detect_any_of_and_regex_and_attachment():
    assert intake._detect_one({"any_of": ["swiggy"]}, "issue on swiggy today", set())
    assert not intake._detect_one({"any_of": ["swiggy"]}, "issue on ubereats", set())
    assert intake._detect_one({"regex": r"\b5\d\d\b"}, "got a 503 error", set())
    assert not intake._detect_one({"regex": r"\b5\d\d\b"}, "no numbers here", set())
    assert intake._detect_one({"attachment_type": "image"}, "", {"image"})
    assert not intake._detect_one({"attachment_type": "image"}, "", {"video"})
    assert not intake._detect_one({"regex": "("}, "unclosed group", set())   # bad regex -> False


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
def test_extract_detect_only_no_llm():
    st = _state(
        case={"sf_id": "500X", "subject": "x",
              "body": "images not visible on swiggy, outlet: BLR-0012"},
        attachments=[{"kind": "image"}],
    )
    ex = intake.extract(IMG_CHECKLIST, st, use_llm=False)
    assert ex["known"]["channels"] == "present"
    assert ex["known"]["outlet_ref"] == "present"
    assert ex["known"]["screenshot"] == "present"
    assert "last_publish" not in ex["known"]        # no detect rule, LLM disabled
    assert ex["sources"]["channels"] == "detect"


def test_extract_reads_existing_sf_field():
    st = _state(sf_context={"case": {"SubModule__c": "Menu Images"}})
    ex = intake.extract(IMG_CHECKLIST, st, use_llm=False)
    assert ex["known"]["outlet_ref"] == "Menu Images"
    assert ex["sources"]["outlet_ref"] == "sf_field"


def test_extract_llm_fills_the_rest(monkeypatch):
    monkeypatch.setattr(
        "interpreter.llm.complete",
        lambda *a, **k: json.dumps(
            {"last_publish": "failed at 3pm", "error_text": "IMG_UPLOAD_403", "channels": "null"}
        ),
    )
    st = _state(case={"sf_id": "500X", "subject": "x",
                      "body": "images not visible on swiggy"})
    ex = intake.extract(IMG_CHECKLIST, st)
    assert ex["known"]["last_publish"] == "failed at 3pm"
    assert ex["known"]["error_text"] == "IMG_UPLOAD_403"
    assert ex["sources"]["last_publish"] == "llm"
    # channels already satisfied by detect -> the "null" from the LLM can't clobber it
    assert ex["known"]["channels"] == "present"


def test_extract_vision_pass_for_vision_signals(monkeypatch):
    calls = []

    def _fake(system, user, **kw):
        calls.append(kw.get("images") is not None)
        if kw.get("images"):
            return json.dumps({"screenshot": "item card shows no photo"})
        return json.dumps({})

    monkeypatch.setattr("interpreter.llm.complete", _fake)
    st = _state(case={"sf_id": "500X", "subject": "x", "body": "images missing"},
                _attachment_blobs={"a": b"\x89PNG..."})
    ex = intake.extract(IMG_CHECKLIST, st)
    assert ex["known"]["screenshot"] == "item card shows no photo"
    assert ex["sources"]["screenshot"] == "vision"
    assert any(calls) and not all(calls)      # one text call, one vision call


def test_extract_never_raises_on_llm_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr("interpreter.llm.complete", boom)
    ex = intake.extract(IMG_CHECKLIST, _state())
    assert isinstance(ex["known"], dict)      # detect hits survive, no exception


# --------------------------------------------------------------------------- #
# gaps / questions / field-writes
# --------------------------------------------------------------------------- #
def test_gaps_are_required_only_and_in_order():
    known = {"channels": "present"}
    g = intake.gaps(IMG_CHECKLIST, known)
    keys = [s["key"] for s in g]
    assert keys == ["outlet_ref", "last_publish", "screenshot"]   # error_text is optional


def test_questions_for_caps_and_dedupes():
    g = intake.gaps(IMG_CHECKLIST, {})
    qs = intake.questions_for(g, limit=3)
    assert len(qs) == 3
    assert qs[0] == "Which channels?"


def test_field_writes_maps_sf_field_and_skips_sentinel_and_ctx():
    known = {
        "channels": "present",                 # ctx-only -> skip
        "outlet_ref": "BLR-0012",              # -> SubModule__c
        "last_publish": "present",             # sentinel + ctx-only -> skip
    }
    assert intake.field_writes(IMG_CHECKLIST, known) == {"SubModule__c": "BLR-0012"}


def test_field_writes_skips_present_sentinel_even_if_field_mapped():
    known = {"outlet_ref": "present"}
    assert intake.field_writes(IMG_CHECKLIST, known) == {}


# --------------------------------------------------------------------------- #
# clarify node wiring (use_checklists)
# --------------------------------------------------------------------------- #
class _ClarifySB:
    """Serves `intake_checklists` from a list, everything else empty."""

    def __init__(self, checklists):
        self._c = checklists

    def table(self, name):
        return _Q(self._c if name == "intake_checklists" else [])


def test_clarify_uses_checklist_questions_when_opted_in(monkeypatch):
    from interpreter.registry import h_clarify

    # LLM would answer the free-form path; assert we DON'T fall back to it.
    monkeypatch.setattr("interpreter.llm.complete",
                        lambda *a, **k: '{"questions": ["FALLBACK?"], "missing": []}')

    state = {
        "tenant_id": "T1",
        "case": {"sf_id": "500XXXXXXXXXXXXXXX", "subject": "Menu image issue",
                 "body": "images not visible on swiggy"},
        "classification": {"topic": "menu-images", "case_type": "Problem / Bug"},
        "attachments": [],
        "confidence": 0.1,
    }
    out = h_clarify(state, {"_node_id": "c", "channel": "email",
                            "use_checklists": True, "dry_run": True,
                            "_sb": _ClarifySB([IMG_CHECKLIST])})

    qs = out["clarification"]["questions"]
    assert "FALLBACK?" not in qs
    assert "Which outlet ID?" in qs            # a checklist gap question
    assert out["clarification"]["checklist"] == "Menu images not visible"
    # `channels` was satisfied by the detect rule -> not asked, not a gap
    assert "channels" not in out["clarification"]["missing"]
    assert out["outcome"]["action"] == "need_info"


def test_clarify_without_opt_in_ignores_checklists(monkeypatch):
    from interpreter.registry import h_clarify

    monkeypatch.setattr("interpreter.llm.complete",
                        lambda *a, **k: '{"questions": ["FALLBACK?"], "missing": []}')
    state = {
        "tenant_id": "T1",
        "case": {"sf_id": "500XXXXXXXXXXXXXXX", "subject": "Menu image issue",
                 "body": "images not visible on swiggy"},
        "classification": {"topic": "menu-images", "case_type": "Problem / Bug"},
        "confidence": 0.1,
    }
    out = h_clarify(state, {"_node_id": "c", "channel": "email",
                            "_sb": _ClarifySB([IMG_CHECKLIST])})
    assert out["clarification"]["questions"] == ["FALLBACK?"]
    assert out["clarification"]["checklist"] is None
