from datetime import datetime, timedelta, timezone

from interpreter.monitor import coaching

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
CFG = {"wrong_raise_labels": ["config-related", "invalid"]}


def issue(reporter="asha", labels=("config-related",), days=2, title="Menu not live"):
    return {"url": f"https://x/{title}", "title": title, "reporter": reporter, "labels": list(labels),
            "closed_at": (NOW - timedelta(days=days)).isoformat()}


def test_the_reporter_is_read_from_the_issue_body():
    body = "**Issue Reporter:** chandan.k | Support \n\n**Service:** Order"
    assert coaching.reporter(body, "Issue Reporter") == "chandan.k"
    assert coaching.reporter("nothing here", "Issue Reporter") is None
    assert coaching.reporter(body, "") is None


def test_the_share_and_reasons_cover_the_last_thirty_days():
    got = coaching.build([issue(), issue(labels=["invalid"]), issue(labels=["confirmed-bug"]),
                          issue(days=40)], CFG, NOW)
    assert (got["closed"], got["wrong"], got["share"]) == (3, 2, 67)
    assert dict(got["reasons"]) == {"config-related": 1, "invalid": 1}


def test_an_agent_shows_only_with_a_pattern():
    got = coaching.build([issue(title="a"), issue(title="b"), issue(labels=["confirmed-bug"], title="c"),
                          issue(reporter="ravi", title="d")], CFG, NOW)
    assert [(a["agent"], a["wrong"], a["raised"]) for a in got["agents"]] == [("asha", 2, 3)]
    assert "2 of your 3" in got["agents"][0]["message"] and "https://x/a" in got["agents"][0]["message"]


def test_an_unknown_reporter_counts_in_the_total_but_nobody_is_named():
    got = coaching.build([issue(reporter=None), issue(reporter=None)], CFG, NOW)
    assert got["wrong"] == 2 and got["agents"] == []


def test_no_issues_is_not_a_division_by_zero():
    assert coaching.build([], CFG, NOW)["share"] == 0
