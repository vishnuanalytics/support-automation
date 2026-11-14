import pytest

from interpreter.monitor import links, redact

GH = "https://github.com/acme/incidents/issues/8645"
SLACK = "https://acme.slack.com/archives/C01ABCDE/p1700000000123456"


def test_bare_github_url():
    assert links.parse(GH) == [{"url": GH, "kind": "github", "owner": "acme",
                                "repo": "incidents", "type": "issues", "number": 8645}]


def test_pull_request_url():
    link = links.parse("https://github.com/acme/hub/pull/12")[0]
    assert (link["type"], link["number"]) == ("pull", 12)


def test_eoc_prefix():
    assert links.parse(f"EOC: {GH}")[0]["number"] == 8645


def test_freshchat_prefix():
    url = "https://web.freshchat.com/a/1/inbox/2"
    assert links.parse(f"Fresh Chat: {url}") == [{"url": url, "kind": "freshchat"}]


def test_slack_and_github_on_two_crlf_lines():
    got = links.parse(f"{SLACK}\r\n{GH}")
    assert [x["kind"] for x in got] == ["slack", "github"]


def test_labelled_slack_url():
    got = links.parse(f"PartnerB escalation thread- {SLACK}")
    assert got[0]["channel"] == "C01ABCDE"


def test_jira():
    url = "https://partner.atlassian.net/browse/DEL-1"
    assert links.parse(f"JIRA: {url}") == [{"url": url, "kind": "jira"}]


@pytest.mark.parametrize("text", ["spoke to the partner manager", "", None])
def test_no_url(text):
    assert links.parse(text) == []


def test_trailing_punctuation_and_dedupe():
    assert links.extract_urls(f"see {GH}. Also ({GH}), {GH};") == [GH]


def test_other_kind():
    assert links.parse("https://example.com/x")[0]["kind"] == "other"


def test_lookalike_hosts_are_not_trusted():
    assert links.parse("https://github.com.evil.io/a/b/issues/1")[0]["kind"] == "other"
    assert links.parse("https://notslack.com/archives/C1/p1700000000123456")[0]["kind"] == "other"


def test_slack_ts_conversion():
    link = links.parse(SLACK)[0]
    assert link["ts"] == "1700000000.123456"
    assert link["thread_ts"] == "1700000000.123456"


def test_slack_thread_ts_param_is_the_parent():
    link = links.parse(f"{SLACK}?thread_ts=1699999999.000100&cid=C01ABCDE")[0]
    assert link["ts"] == "1700000000.123456"
    assert link["thread_ts"] == "1699999999.000100"


def test_github_key_is_case_insensitive():
    a = links.parse("https://github.com/Acme/Incidents/issues/1")[0]
    b = links.parse("https://github.com/acme/incidents/issues/1#issuecomment-9")[0]
    assert links.github_key(a) == links.github_key(b)


def test_signed_params_removed_and_others_kept():
    url = ("https://s3.amazonaws.com/b/f.png?Signature=abc&AWSAccessKeyId=KEY"
           "&X-Amz-Credential=c&X-Amz-Signature=s&Expires=1&keep=1")
    assert redact.strip_signed_url(url) == "https://s3.amazonaws.com/b/f.png?keep=1"


def test_signed_url_inside_text():
    out = redact.clean("see https://x.io/a?Signature=sekret&AWSAccessKeyId=K now")
    assert "sekret" not in out and "AWSAccessKeyId" not in out and out.endswith("now")


def test_links_strip_signed_params():
    assert links.extract_urls("https://x.io/a?X-Amz-Signature=s") == ["https://x.io/a"]


def test_truncates_stored_text():
    out = redact.clean("a" * 500)
    assert len(out) == 401 and out.endswith("…")


def test_redact_pii():
    out = redact.redact_pii("Mail a.b@x.co call +91 98765 43210 ifsc HDFC0001234 "
                            "card 4111 1111 1111 1111")
    for leaked in ("a.b@x.co", "98765", "HDFC0001234", "4111"):
        assert leaked not in out
