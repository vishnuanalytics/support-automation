"""One sync: list cases, read their links, compute verdicts, record changes.

A listing failure fails the whole sync and changes nothing, so one bad pull
can't mark every case closed. A failure on one link or timeline is recorded
against that case and the sync carries on.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from . import issue_status, replies, verdict
from .readers import CONCURRENCY, EvidenceCollector, merge_links

log = logging.getLogger("interpreter.monitor.sync")


def counts(rows: list[dict]) -> dict[str, int]:
    v = [r["verdict"] for r in rows]
    return {"total": len(v),
            "my_turn": sum(x["ball"] == "us" for x in v),
            "mismatches": sum(bool(x["mismatch"]) for x in v),
            "missing_link": sum(bool(x["missing_link"]) for x in v),
            "not_raised": sum(bool(x.get("not_raised")) for x in v),
            "overdue": sum(bool(x["overdue"]) for x in v),
            "critical": sum(x["level"] == "critical" for x in v)}


def _is_after(a: str | None, b: str | None) -> bool:
    x, y = verdict.parse_dt(a), verdict.parse_dt(b)
    return bool(x and (y is None or x > y))


def diff_events(prior: dict[str, dict], rows: list[dict]) -> list[dict]:
    """What changed since the last sync, for the digest. The very first sync
    has nothing to compare with, so it reports nothing."""
    if not prior:
        return []
    out: list[dict] = []
    for row in rows:
        case, v = row["case"], row["verdict"]
        old = prior.get(case["id"])
        base = {"case_id": case["id"], "case_number": case.get("case_number"),
                "subject": case.get("subject")}
        if old is None:
            out.append({**base, "kind": "new_case"})
            continue
        before = old.get("verdict") or {}
        detail = (v.get("signal") or {}).get("text")
        acts_now = verdict.stage(v) == "act"
        was_acting = before.get("ball") == verdict.US
        same_reason = (before.get("signal") or {}).get("code") == (v.get("signal") or {}).get("code")
        if acts_now and not (was_acting and same_reason):
            out.append({**base, "kind": "needs_action", "detail": detail})
        elif _is_after(v.get("latest_reply_at"), before.get("latest_reply_at")) and \
                (v.get("signal") or {}).get("code") != "third_party_ack":
            out.append({**base, "kind": "new_reply", "detail": detail})
        if v.get("stale") and not before.get("stale") and not acts_now:
            out.append({**base, "kind": "newly_stale", "detail": v.get("next_action")})
        if v["overdue"] and not before.get("overdue"):
            out.append({**base, "kind": "newly_overdue"})
        if v["missing_link"] and not before.get("missing_link"):
            out.append({**base, "kind": "newly_missing_link"})
    return out


def _with_ai_feed(cases: list[dict], ai: dict[str, dict], reader: Any,
                  errors: list[dict]) -> list[dict]:
    by_id = {c["id"]: c for c in cases}
    missing = [i for i in ai if i not in by_id]
    if missing:
        try:
            for c in reader.get_cases(missing):
                by_id[c["id"]] = c
        except Exception as e:  # noqa: BLE001
            errors.append({"error": f"could not load AI-handled cases ({type(e).__name__})"})
    for cid, info in ai.items():
        case = by_id.setdefault(cid, {
            "id": cid, "source": "ai", "subject": info.get("subject"),
            "last_modified_at": info.get("at"), "created_at": info.get("at"),
            "links": [], "third_parties": []})
        case["ai"] = info
    return list(by_id.values())


def _timelines(cases: list[dict], prior: dict[str, dict], reader: Any,
               errors: list[dict]) -> dict[str, dict]:
    """Only cases that changed since their timeline was last fetched."""
    def stale(c: dict) -> bool:
        old = prior.get(c["id"])
        if not old or not old.get("timeline"):
            return True
        return c.get("last_modified_at") != (old.get("case") or {}).get("last_modified_at")

    todo = [c for c in cases if c.get("source") != "ai" and stale(c)]

    def one(case: dict) -> tuple[str, dict | None]:
        try:
            return case["id"], reader.timeline(case["id"])
        except Exception as e:  # noqa: BLE001
            errors.append({"case_id": case["id"], "case_number": case.get("case_number"),
                           "error": f"timeline not read ({type(e).__name__})"})
            return case["id"], None

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        return {cid: t for cid, t in pool.map(one, todo) if t is not None}


def _enrich(cases: list[dict], reader: Any, errors: list[dict]) -> None:
    try:
        children = reader.children_of([c["id"] for c in cases if c.get("source") != "ai"])
        for c in cases:
            c["children"] = children.get(c["id"], [])
            c["links"] = merge_links(c.get("links") or [],
                                     *[x.pop("links", []) for x in c["children"]])
    except Exception as e:  # noqa: BLE001
        errors.append({"error": f"child cases not read ({type(e).__name__})"})
    try:
        stats = reader.brand_stats(sorted({c["brand"] for c in cases if c.get("brand")}))
        for c in cases:
            c["brand_stats"] = stats.get(c.get("brand"))
    except Exception as e:  # noqa: BLE001
        errors.append({"error": f"brand counts not read ({type(e).__name__})"})


def _post_links(cases: list[dict], reader: Any, errors: list[dict]) -> None:
    """When the link field is empty, the link may be in the case's posts."""
    empty = [c for c in cases if c.get("source") != "ai" and not c.get("links")]
    ids = sorted({i for c in empty for i in (c["id"], (c.get("parent") or {}).get("id")) if i})
    if not ids:
        return
    try:
        found = reader.post_links(ids)
    except Exception as e:  # noqa: BLE001
        errors.append({"error": f"case posts not read ({type(e).__name__})"})
        return
    for c in empty:
        c["links"] = merge_links(found.get(c["id"], []),
                                 found.get((c.get("parent") or {}).get("id"), []))


def _dsp_ids(case: dict, record_type: str) -> list[str]:
    """The cases of the raising record type linked to this one, in either direction."""
    related = [*(c for c in [case.get("parent")] if c), *(case.get("children") or [])]
    return [x["id"] for x in related if x.get("record_type") == record_type]


def _attach_partners(cases: list[dict], partners: list[dict], cfg: dict) -> None:
    """Add each third-party case's directory entry and whether a DSP case exists."""
    index: dict[str, dict] = {}
    for p in partners:
        for name in [p["name"], *(p.get("aliases") or [])]:
            index[name.lower()] = p
    record_type = cfg.get("dependency_record_type")
    for c in cases:
        if c.get("waiting_party") != verdict.THIRD_PARTY:
            continue
        c["partner"] = next((index[n.lower()] for n in c.get("third_parties") or []
                             if n.lower() in index), None)
        c["has_dsp"] = not record_type or c.get("record_type") == record_type or bool(
            _dsp_ids(c, record_type))


EMAILS_WITH_TEXT = 5


def _domains(text: str | None) -> list[str]:
    return [a.lower() for a in re.findall(r"@([\w.-]+\.\w+)", text or "")]


def _with_partner(messages: list[dict], domains: list[str]) -> list[dict]:
    """A non-DSP case's emails are mostly with the customer, so keep only those
    to or from the third party's own domains."""
    return [m for m in messages
            if any(verdict.in_domains(d, domains)
                   for d in _domains(m.get("who")) + _domains(m.get("to")))]


def _email_log(messages: list[dict]) -> list[dict]:
    """Who wrote and when, without the text: enough to tell how long someone's been quiet."""
    return [{"who": m.get("who"), "at": m.get("at"), "party": m["party"], "ours": m["ours"],
             "ack": replies.rule_kind(m.get("text")) is not None}
            for m in messages if m["party"] != verdict.CUSTOMER]


def _dsp_emails(cases: list[dict], reader: Any, cfg: dict, errors: list[dict]) -> None:
    """Attach third-party email follow-ups: all emails on the record type used to
    raise them, on the case or the case linked to it, plus any email to or from
    the third party on the case itself."""
    record_type = cfg.get("dependency_record_type")
    if not record_type:
        return
    wanted: dict[str, list[str]] = {}
    for c in cases:
        if c.get("waiting_party") != verdict.THIRD_PARTY:
            continue
        ids = [c["id"]]
        if c.get("record_type") != record_type:
            ids += _dsp_ids(c, record_type)
        wanted[c["id"]] = ids
    ids = sorted({i for group in wanted.values() for i in group})
    if not ids:
        return
    try:
        emails = reader.latest_emails(ids)
    except Exception as e:  # noqa: BLE001
        errors.append({"error": f"DSP emails not read ({type(e).__name__})"})
        return
    for c in cases:
        group = wanted.get(c["id"])
        if not group:
            continue
        def keep(found: list[dict]) -> list[dict]:
            if c.get("record_type") == record_type:
                return found
            return _with_partner(found, (c.get("partner") or {}).get("email_domains") or [])

        def classify(found: list[dict]) -> list[dict]:
            parties = [verdict.email_party(m.get("who"), not m.get("ours"), c, cfg) for m in found]
            return [{**m, "party": p, "ours": p == verdict.US, "url": c.get("url")}
                    for m, p in zip(found, parties)]

        own = emails.get(c["id"], [])
        linked = [emails.get(i, []) for i in group[1:]]
        messages = classify(keep(own[:EMAILS_WITH_TEXT])
                            + [m for found in linked for m in found[:EMAILS_WITH_TEXT]])
        log = _email_log(classify(keep(own) + [m for found in linked for m in found]))
        if log:
            c["email_log"] = log
        if messages:
            c["email_evidence"] = {"kind": "email", "url": f"email:{c['id']}", "ok": True,
                                   "label": "DSP email", "messages": messages,
                                   "related": any(linked) or c.get("record_type") != record_type}


def case_emails(case: dict, raw: dict | None, cfg: dict) -> dict:
    """The case's own emails, so a row's last response can't miss a customer email."""
    messages = []
    for e in (raw or {}).get("emails") or []:
        party = verdict.email_party(e.get("from"), bool(e.get("incoming")), case, cfg)
        messages.append({"who": e.get("from"), "party": party, "ours": party == verdict.US,
                         "at": e.get("at"), "text": e.get("text") or e.get("subject"),
                         "url": case.get("url")})
    return {"kind": "email", "ok": True, "label": "Case email", "url": case.get("url"),
            "messages": messages}


def _read_issues(tenant_id: str, cases: list[dict], collector: EvidenceCollector, store: Any,
                 cfg: dict, errors: list[dict], now: datetime) -> None:
    """Keep the Engineering page's list: every open labelled issue, plus any a case links to."""
    github = collector.readers.get("github")
    if github is None:
        return
    linked = [x for c in cases for x in c.get("links") or [] if x["kind"] == "github"]
    listed: list[dict] = []
    complete = True
    for owner, repo in sorted({(x["owner"], x["repo"]) for x in linked}):
        try:
            listed += github.open_issue_links(owner, repo, cfg["github_support_label"])
        except Exception as e:  # noqa: BLE001 -- a partial list must not close issues
            complete = False
            errors.append({"error": f"GitHub issues not listed for {repo} ({type(e).__name__})"})
    found = collector.collect(listed)
    # A case can link an issue by another URL form, so one issue is keyed by its number.
    by_issue = {issue_status.issue_key(x): found[x["url"]] for x in linked + listed}
    current = {ev["url"]: ev for ev in by_issue.values()}
    issues = [ev for ev in current.values() if ev.get("ok") and ev.get("state") == "open"]
    known = store.issue_reads(tenant_id)
    reads = issue_status.read_all(tenant_id, issues, known, cfg)
    # An issue that failed to load keeps its row, so the page can say it wasn't read.
    missing = {url: {"status": issue_status.NOT_READ, "method": "none"}
               for url, ev in current.items() if not ev.get("ok") and url not in known}
    store.save_issues(tenant_id, {**reads, **missing}, now)
    if complete:
        kept = {url for url, ev in current.items() if not ev.get("ok") or ev.get("state") == "open"}
        store.close_issues(tenant_id, [url for url, row in known.items()
                                       if row.get("is_open") and url not in kept])


def run(tenant_id: str, store: Any, reader: Any, evidence_readers: dict, cfg: dict,
        *, now: datetime | None = None, trigger: str = "schedule") -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    sync_id = store.start_sync(tenant_id, trigger)
    if sync_id is None:
        return {"skipped": "a sync is already running"}
    errors: list[dict] = []
    try:
        cases = _with_ai_feed(reader.list_dependency_cases(), store.ai_feed(tenant_id), reader,
                              errors)
        prior = store.open_cases(tenant_id)
        _enrich(cases, reader, errors)
        _post_links(cases, reader, errors)
        _attach_partners(cases, store.partners(tenant_id), cfg)
        _dsp_emails(cases, reader, cfg, errors)
        collector = EvidenceCollector(evidence_readers)
        found = collector.collect([x for c in cases for x in c.get("links") or []])
        # A thread the sync can't read keeps its last import, not an error.
        imported = store.imported_evidence(tenant_id)
        found.update({url: imported[url] for url, ev in found.items()
                      if not ev.get("ok") and url in imported})
        try:
            _read_issues(tenant_id, cases, collector, store, cfg, errors, now)
        except Exception as e:  # noqa: BLE001 -- the case queue doesn't depend on this
            log.exception("issue read failed for %s", tenant_id)
            errors.append({"error": f"GitHub issues not read ({type(e).__name__})"})
        timelines = _timelines(cases, prior, reader, errors)

        rows = []
        evidence_by_case = {}
        for case in cases:
            evidence = [found[x["url"]] for x in case.get("links") or []]
            if case.get("email_evidence"):
                evidence.append(case["email_evidence"])
            for ev in evidence:
                if ev.get("error"):
                    errors.append({"case_id": case["id"], "case_number": case.get("case_number"),
                                   "url": ev["url"], "error": ev["error"]})
            evidence_by_case[case["id"]] = evidence
        replies.label_latest(tenant_id, [(c, verdict.latest_message(evidence_by_case[c["id"]]))
                                         for c in cases], store, cfg)
        for case in cases:
            evidence = evidence_by_case[case["id"]]
            v = verdict.compute(case, evidence, cfg, now)
            raw = timelines.get(case["id"]) or (prior.get(case["id"]) or {}).get("timeline")
            v["latest"] = verdict.latest_message(
                evidence + [case_emails(case, raw, cfg)])
            rows.append({"case": case, "verdict": v, "timeline": timelines.get(case["id"])})

        store.save_cases(tenant_id, rows, now)
        store.save_evidence(tenant_id, list(found.values()), now)
        seen = {c["id"] for c in cases}
        store.close_cases(tenant_id, [i for i in prior if i not in seen])
        store.add_events(tenant_id, sync_id, diff_events(prior, rows))

        since = store.last_resolved_at(tenant_id) or (
            now - timedelta(days=int(cfg["resolved_lookback_days"])))
        try:
            store.save_resolved(tenant_id, reader.resolved_since(
                since.strftime("%Y-%m-%dT%H:%M:%SZ")))
        except Exception as e:  # noqa: BLE001
            errors.append({"error": f"resolved cases not read ({type(e).__name__})"})

        result = counts(rows)
        store.finish_sync(sync_id, "partial" if errors else "ok", result, errors)
        return {"sync_id": sync_id, "status": "partial" if errors else "ok", **result,
                "errors": errors}
    except Exception as e:  # noqa: BLE001
        log.exception("monitor sync failed for %s", tenant_id)
        message = str(e) if isinstance(e, ValueError) else f"sync failed ({type(e).__name__})"
        store.finish_sync(sync_id, "failed", {}, [{"error": message}])
        return {"sync_id": sync_id, "status": "failed", "error": message}
