import json

from interpreter.monitor import debug, mcp_server

TIMELINE = [
    {"party": "third_party", "party_label": "Aggregator", "source": "Email", "url": None,
     "at": "2026-11-17T10:00:00", "text": "Menu publish failed with error 4001 for store 55"},
    {"party": "system", "text": "Owner changed"},
]
CHECKS = [{"check_name": "MNU-01 Publish history", "summary": "Last publish FAILED", "row_count": 1,
           "rows": [{"status": "FAILED"}]},
          {"check_name": debug.INVESTIGATION, "summary": "old", "rows": []}]


def test_sources_skip_system_and_old_writeup():
    src = debug.build_sources(TIMELINE, CHECKS)
    assert [s["source"] for s in src] == ["Email", "Saved check: MNU-01 Publish history"]
    assert "FAILED" in src[1]["text"]


def answer(**kw):
    body = {"root_cause": "Publish rejected", "not_verified": ["Aggregator dashboard"],
            "next_action": "Re-push the menu", "draft_reply": "We are re-pushing.",
            "evidence": [{"claim": "Publish failed", "source": 1, "quote": "error 4001"}]}
    body.update(kw)
    return json.dumps(body)


def test_quote_found_in_source_is_proof():
    src = debug.build_sources(TIMELINE, CHECKS)
    out = debug.parse(answer(), src)
    assert out["evidence"][0]["source_name"] == "Email"
    assert out["not_verified"] == ["Aggregator dashboard"]


def test_invented_quote_or_bad_source_moves_to_not_verified():
    src = debug.build_sources(TIMELINE, CHECKS)
    out = debug.parse(answer(evidence=[
        {"claim": "A", "source": 1, "quote": "invented words"},
        {"claim": "B", "source": 9, "quote": "error 4001"},
        {"claim": "C", "source": "x", "quote": "error 4001"}]), src)
    assert out["evidence"] == []
    assert len(out["not_verified"]) == 4


def test_rows_round_trip():
    src = debug.build_sources(TIMELINE, CHECKS)
    out = debug.parse(answer(), src)
    check = {"summary": out["root_cause"], "rows": debug.to_rows(out), "saved_by": "x", "created_at": "t"}
    back = debug.from_check(check)
    assert back["root_cause"] == "Publish rejected"
    assert back["evidence"][0]["quote"] == "error 4001"
    assert back["next_action"] == "Re-push the menu"
    assert back["draft_reply"] == "We are re-pushing."


def test_investigation_row_keeps_unsupported_claims_as_not_verified():
    row = mcp_server.investigation_row(
        "t", "c", "Publish rejected",
        [{"claim": "ok", "source": "ORD-02", "quote": "CANCELLED"}, {"claim": "guess"}],
        ["logs"], "Re-push", "Hi", "Claude session")
    assert row["check_name"] == debug.INVESTIGATION and row["summary"] == "Publish rejected"
    back = debug.from_check(row)
    assert [e["claim"] for e in back["evidence"]] == ["ok"]
    assert "guess (no source or quote given)" in back["not_verified"]


def test_investigation_requires_root_cause():
    import pytest
    with pytest.raises(ValueError):
        mcp_server.investigation_row("t", "c", " ", [], [], "", "", "x")


def test_investigations_due_skips_cases_already_written_up(monkeypatch):
    from interpreter.monitor import service

    def item(cid, ball, activity):
        return ({"id": cid, "case_number": cid, "subject": "s", "owner": "o", "third_parties": []},
                {"ball": ball, "latest_activity_at": activity}, [])

    monkeypatch.setattr(service, "load_config", lambda sb, t: {})
    monkeypatch.setattr(service, "_cases_with_evidence", lambda sb, t, cfg: [
        item("new", "us", "2026-11-18T10:00:00+00:00"),
        item("done", "us", "2026-11-18T09:00:00+00:00"),
        item("stale", "us", "2026-11-18T11:00:00+00:00"),
        item("theirs", "engineering", "2026-11-18T12:00:00+00:00")])
    monkeypatch.setattr(service, "checks_by_case", lambda sb, t, ids: {
        "done": [{"check_name": debug.INVESTIGATION, "created_at": "2026-11-18T09:30:00+00:00"}],
        "stale": [{"check_name": debug.INVESTIGATION, "created_at": "2026-11-18T08:00:00+00:00"}]})
    out = service.investigations_due(None, "t")
    assert [c["case_number"] for c in out["cases"]] == ["stale", "new"]
    assert out["cases"][0]["had_investigation"] is True


def test_debug_case_returns_checked_evidence(monkeypatch):
    from interpreter.monitor import service
    detail = {"llm_enabled": True, "case": {"subject": "s"}, "verdict": {"ball": "us"},
              "timeline": TIMELINE, "checks": CHECKS}
    monkeypatch.setattr(service, "case_detail", lambda sb, t, c: detail)
    out = service.debug_case(None, "t", "c", complete=lambda *a, **k: answer())
    assert out["enabled"] and out["evidence"][0]["quote"] == "error 4001"
    detail["llm_enabled"] = False
    assert service.debug_case(None, "t", "c") == {"enabled": False}
