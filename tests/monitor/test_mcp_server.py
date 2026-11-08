"""Case Monitor MCP server: the shaping is pure, the tools are thin (offline)."""
import pytest

from interpreter.monitor import mcp_server as m


def _detail(**case_kw):
    case = {"id": "500x", "case_number": "00000042", "subject": "Images missing", "owner": "Asha",
            "description": "x" * 5000, "third_parties": ["PartnerA"], "created_at": "2026-11-01T10:00:00Z",
            "location_id": "1001", "biz_id": "", "links": [{"kind": "github", "url": "https://gh/1", "extra": 1}],
            **case_kw}
    verdict = {"ball": "us", "ready_to_close": True, "level": "high", "score": 45, "next_action": "Close it",
               "reasons": [{"code": "dsp_resolved", "text": "DSP resolved", "points": 0,
                            "source": {"label": "Salesforce: case 9", "url": "u"}}]}
    timeline = [{"at": f"2026-11-0{i % 9 + 1}", "source": "Email", "party": "customer", "who": "m",
                 "text": "hi", "url": "u", "party_label": "Merchant"} for i in range(m.TIMELINE_CAP + 5)]
    return {"case": case, "verdict": verdict, "timeline": timeline, "timeline_at": "2026-11-08T17:00:00Z",
            "evidence": [{"kind": "email", "label": "DSP email", "url": "e", "ok": True, "messages": [1, 2]}],
            "similar": [{"case_number": str(i), "resolution_note": "fixed " * 300, "match_count": 2}
                        for i in range(8)],
            "duplicates": [], "brief": {"problem": {"text": "Images missing"}}}


def test_check_inputs_take_ids_platform_and_case_date_and_skip_blanks():
    got = m.check_inputs(_detail()["case"])
    assert got == {"location_id": "1001", "platform": "PartnerA", "case_date": "2026-11-01"}


def test_pack_marks_text_as_untrusted_and_caps_long_text():
    got = m.pack(_detail())
    assert got["note"] == m.UNTRUSTED
    assert len(got["case"]["description"]) < 1300 and "more characters" in got["case"]["description"]
    assert got["timeline"]["shown"] == m.TIMELINE_CAP and got["timeline"]["total"] == m.TIMELINE_CAP + 5
    assert len(got["similar_resolved"]) == m.SIMILAR_CAP
    assert len(got["similar_resolved"][0]["resolution_note"]) < 700


def test_pack_reports_the_stage_and_cites_reasons():
    got = m.pack(_detail())
    assert got["verdict"]["stage"] == "close" and got["verdict"]["ready_to_close"]
    assert got["verdict"]["reasons"] == [{"text": "DSP resolved", "points": 0, "source": "Salesforce: case 9"}]
    assert got["case"]["links"] == [{"kind": "github", "url": "https://gh/1"}]
    assert got["linked_items"][0] == {"kind": "email", "label": "DSP email", "url": "e", "state": None,
                                      "readable": True, "error": None, "messages": 2}


@pytest.mark.parametrize("query,want", [("", True), ("00000042", True), ("asha", True),
                                        ("partnera", True), ("partnerb", False)])
def test_matches(query, want):
    item = {"case_number": "00000042", "subject": "Images", "owner": "Asha", "third_parties": ["PartnerA"]}
    assert m.matches(item, query) is want


class _Query:
    def __init__(self, rows, log):
        self.rows, self.log = rows, log

    def select(self, *_):
        return self

    def eq(self, col, val):
        self.log.append((col, val))
        self.rows = [r for r in self.rows if r.get(col, r.get("_" + col)) == val]
        return self

    def limit(self, _):
        return self

    def execute(self):
        return type("R", (), {"data": self.rows})()


class _SB:
    def __init__(self, rows):
        self.rows, self.log = rows, []

    def table(self, _):
        return _Query(list(self.rows), self.log)


def test_resolve_tenant_uses_the_only_enabled_workspace(monkeypatch):
    monkeypatch.delenv("MONITOR_TENANT_ID", raising=False)
    sb = _SB([{"tenant_id": "a", "config": {"enabled": True}}, {"tenant_id": "b", "config": {}}])
    assert m.resolve_tenant(sb) == "a"


def test_resolve_tenant_refuses_to_guess(monkeypatch):
    monkeypatch.delenv("MONITOR_TENANT_ID", raising=False)
    sb = _SB([{"tenant_id": "a", "config": {"enabled": True}}, {"tenant_id": "b", "config": {"enabled": True}}])
    with pytest.raises(RuntimeError, match="MONITOR_TENANT_ID"):
        m.resolve_tenant(sb)
    monkeypatch.setenv("MONITOR_TENANT_ID", "b")
    assert m.resolve_tenant(sb) == "b"


def test_find_case_id_accepts_an_id_or_an_unpadded_case_number():
    sb = _SB([{"tenant_id": "t", "case_id": "500x", "case_data->>case_number": "00000099"}])
    assert m.find_case_id(sb, "t", "500x") == "500x"
    assert m.find_case_id(sb, "t", "#99") == "500x"
    assert m.find_case_id(sb, "t", "") is None


def test_check_row_keeps_the_first_50_rows_but_counts_them_all():
    rows = [[i] for i in range(80)]
    got = m.check_row("t", "500x", "  MNU-01 Publish history ", 7300, {"location_id": 1}, "All ok", rows, "me")
    assert got["check_name"] == "MNU-01 Publish history" and got["card_id"] == "7300"
    assert got["row_count"] == 80 and len(got["rows"]) == m.CHECK_ROWS_CAP and got["rows"][0] == [0]
    assert got["tenant_id"] == "t" and got["case_id"] == "500x" and got["saved_by"] == "me"


def test_check_row_defaults_and_caps():
    got = m.check_row("t", "c", "X", "", None, "s" * 5000, None, "me")
    assert got["params"] == {} and got["rows"] == [] and got["row_count"] == 0 and got["card_id"] is None
    assert len(got["summary"]) < 1100
    assert m.check_row("t", "c", "X", None, {}, "  ", [], "me")["summary"] is None


@pytest.mark.parametrize("kw,msg", [({"check_name": " "}, "check_name"),
                                    ({"params": ["a"]}, "params"),
                                    ({"rows": "nope"}, "rows")])
def test_check_row_rejects_bad_input(kw, msg):
    args = {"check_name": "X", "params": {}, "rows": []} | kw
    with pytest.raises(ValueError, match=msg):
        m.check_row("t", "c", args["check_name"], None, args["params"], "s", args["rows"], "me")


def test_saved_check_view_leaves_out_rows_and_ids():
    row = {"id": "u", "tenant_id": "t", "case_id": "c", "check_name": "X", "card_id": "1", "params": {},
           "row_count": 3, "summary": "s", "rows": [[1]], "saved_by": "me", "created_at": "2026-11-08"}
    assert m.saved_check_view(row) == {"check_name": "X", "card_id": "1", "params": {}, "row_count": 3,
                                       "summary": "s", "saved_by": "me", "created_at": "2026-11-08"}


def test_check_row_labels_list_rows_when_columns_are_given():
    got = m.check_row("t", "c", "X", None, {}, "s", [[1, "a"], {"k": 2}], "me", columns=["id", "name"])
    assert got["rows"] == [{"id": 1, "name": "a"}, {"k": 2}]


class _ChecksQuery(_Query):
    def order(self, col, desc=False):
        self.log.append(("order", col, desc))
        return self

    def select(self, cols):
        self.log.append(("select", cols))
        return self


def test_case_checks_reads_newest_first_for_one_case_and_can_skip_rows():
    from interpreter.monitor import service
    log = []
    sb = type("SB", (), {"table": lambda self, name: (log.append(("table", name)),
              _ChecksQuery([{"tenant_id": "t", "case_id": "c", "check_name": "X"},
                            {"tenant_id": "t", "case_id": "other", "check_name": "Y"}], log))[1]})()
    assert [r["check_name"] for r in service.case_checks(sb, "t", "c")] == ["X"]
    assert ("table", "monitor_case_checks") in log and ("order", "created_at", True) in log
    assert any(e[0] == "select" and e[1].endswith(", rows") for e in log)
    log.clear()
    service.case_checks(sb, "t", "c", with_rows=False)
    assert all(not e[1].endswith(", rows") for e in log if e[0] == "select")
