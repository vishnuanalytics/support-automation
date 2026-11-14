import json

import pytest

from interpreter.monitor import issue_status as st

URL = "https://github.com/o/r/issues/1"


def msg(text="It's the printer", ours=False, who="dev", at="2026-11-01T00:00:00Z"):
    return {"who": who, "at": at, "ours": ours, "text": text}


def issue(messages=(), **kw):
    return {"url": URL, "title": "Printer down", "body": "KOT not printing", "messages": list(messages), **kw}


def answer(status="answered", waiting_on="nobody", reason="The printer was removed"):
    return lambda *a, **k: json.dumps({"status": status, "waiting_on": waiting_on, "reason": reason})


def read(ev, known=None, complete=None, cfg=None, clock=None):
    kw = {"clock": clock} if clock else {}
    return st.read_all("t", [ev], known or {}, cfg or {"llm_enabled": True}, complete=complete, **kw)[URL]


def test_only_support_comments_is_no_tech_reply_without_paying_for_a_read():
    def fail(*a, **k):
        raise AssertionError("no LLM call expected")

    got = read(issue([msg("Any update?", ours=True)]), complete=fail)
    assert (got["status"], got["waiting_on"], got["method"]) == (st.NO_REPLY, "tech", "rule")


def test_no_comments_at_all_is_no_tech_reply():
    assert read(issue())["status"] == st.NO_REPLY


def test_a_tech_comment_is_read():
    got = read(issue([msg()]), complete=answer())
    assert (got["status"], got["waiting_on"], got["reason"], got["method"]) == (
        st.ANSWERED, "nobody", "The printer was removed", "llm")


def test_an_unchanged_thread_is_not_read_again():
    ev = issue([msg()])
    known = {URL: {"thread_hash": st.thread_hash(ev), "method": "llm", "status": st.CAUSE_GIVEN,
                   "waiting_on": "merchant", "reason": "Merchant to confirm"}}

    def fail(*a, **k):
        raise AssertionError("no LLM call expected")

    got = read(ev, known, complete=fail)
    assert (got["status"], got["reason"]) == (st.CAUSE_GIVEN, "Merchant to confirm")


def test_a_new_comment_means_a_new_read():
    old = issue([msg()])
    known = {URL: {"thread_hash": st.thread_hash(old), "method": "llm", "status": st.INVESTIGATING}}
    assert read(issue([msg(), msg("Fix is live", at="2026-11-02T00:00:00Z")]), known,
                complete=answer())["status"] == st.ANSWERED


def test_a_failed_read_is_not_kept_as_a_reading_and_is_retried():
    ev = issue([msg()])
    known = {URL: {"thread_hash": st.thread_hash(ev), "method": "none", "status": st.NOT_READ}}
    assert read(ev, known, complete=answer())["status"] == st.ANSWERED


@pytest.mark.parametrize("bad", [
    lambda *a, **k: "not json",
    answer(status="resolved"),
    answer(status="no_reply"),  # settled by rule: an outside comment is always a tech reply
    lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rate limited")),
])
def test_a_bad_answer_shows_as_not_read_never_as_answered(bad):
    got = read(issue([msg()]), complete=bad)
    assert (got["status"], got["method"]) == (st.NOT_READ, "none")


def test_with_the_ai_switched_off_a_tech_comment_shows_as_not_read():
    assert read(issue([msg()]), cfg={"llm_enabled": False}, complete=answer())["status"] == st.NOT_READ


def test_running_out_of_time_leaves_the_rest_not_read():
    ticks = iter([0, st.READ_SECONDS_PER_SYNC + 1])
    assert read(issue([msg()]), complete=answer(), clock=lambda: next(ticks))["status"] == st.NOT_READ


def test_an_unknown_waiting_on_is_dropped():
    assert read(issue([msg()]), complete=answer(waiting_on="the moon"))["waiting_on"] is None


def test_the_thread_marks_who_wrote_what_and_removes_contact_details():
    text = st.thread_text(issue([msg("Call me at a@b.com"), msg("Chasing", ours=True, who="agent")]))
    assert "[other] dev" in text and "[support] agent" in text
    assert "a@b.com" not in text and "Printer down" in text and "KOT not printing" in text


def test_a_long_thread_keeps_the_newest_comments_in_order():
    messages = [msg(f"c{i} " + "x" * 1400) for i in range(10)]
    text = st.thread_text(issue(messages))
    assert "c9 " in text and "c0 " not in text and "earlier comments left out" in text
    assert text.index("c8 ") < text.index("c9 ")
