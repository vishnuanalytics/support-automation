"""Which saved data checks a new aggregator or engineering case should get, and what they found. Pure.

The app can't reach the BI tool, so it only decides. A scheduled Claude routine
asks for the due list (MCP `checks_due`), runs each saved question and records
the result with `save_check`. A check with a saved result is done, so nothing
runs twice; one that still lacks an input waits and is listed as blocked.

What a check is for (an order lookup, a stock timeline) lives here. Which saved
question does it, and which cases get which checks, is per-tenant config:
`check_questions` and `auto_checks`.

Results are read by rule only from their row count: "found nothing" is the one
reading that needs no knowledge of the question's columns. It feeds a Needs Me
card, never a verdict.
"""

from __future__ import annotations

from datetime import timedelta, timezone

from . import case_details
from .verdict import parse_dt

WINDOW_BEFORE_DAYS = 2  # stock, menu and store logs are read from this long before the case
DUE_LIMIT = 20

# needs: inputs the case must give; optional: given when known; after: a check whose
# result must be saved first, with rows; fill: inputs the routine reads from that result.
KINDS: dict[str, dict] = {
    "order": {"needs": ["order_id", "case_date"], "optional": ["biz_id", "location_id", "platform"],
              "empty": "No order found with this ID"},
    "order_status": {"after": "order", "fill": {"up_order_id": "the platform's own order ID in the order result"},
                     "empty": "No status changes recorded for the order"},
    "order_items": {"after": "order",
                    "fill": {"order_id": "the order ID in the order result",
                             "order_date": "the order's date in the order result",
                             "biz_id": "the business ID in the order result"},
                    "empty": "No items recorded for the order"},
    "item_stock": {"needs": ["location_id", "platform"], "optional": ["from"], "after": "order_items",
                   "fill": {"item_id": "the ID of the item the case names, from the items result"},
                   "empty": "No stock change logged for the item"},
    "menu_publish": {"needs": ["location_id", "platform"], "optional": ["from"],
                     "empty": "No menu publish in the window"},
    "store_actions": {"needs": ["location_id", "platform"], "optional": ["from"],
                      "empty": "No store on/off action in the window"},
}
LABELS = {"order_id": "order ID", "case_date": "case date", "location_id": "location ID",
          "platform": "aggregator name"}


def rule_for(case: dict, rules: list[dict]) -> dict | None:
    """First match wins, by sub-module, then module."""
    for r in rules:
        if case.get("sub_module") in (r.get("sub_modules") or []) or case.get("module") in (r.get("modules") or []):
            return r
    return None


def is_engineering(case: dict) -> bool:
    return case.get("waiting_party") == "engineering" or any(
        x.get("kind") == "github" for x in case.get("links") or [])


def _has_dsp(case: dict, cfg: dict) -> bool:
    refs = [case.get("parent") or {}, *(case.get("children") or [])]
    return any(r.get("record_type") == cfg.get("dependency_record_type") for r in refs)


def engineering_items(items: list[tuple], cfg: dict) -> list[tuple]:
    """Open engineering cases without their own aggregator case, when that scan is on."""
    if not cfg.get("auto_checks_engineering"):
        return []
    return [i for i in items if i[0].get("record_type") != cfg.get("dependency_record_type")
            and is_engineering(i[0]) and not _has_dsp(i[0], cfg)]


def _given(r: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in r["params"].items() if k in ("order_id", "location_id"))


def notes(rows: list[dict]) -> dict[str, list[str]]:
    """found_nothing rows as "please check" lines per case."""
    out: dict[str, list[str]] = {}
    for r in rows:
        given = _given(r)
        out.setdefault(r["case_id"], []).append(
            f"{r['check']} found nothing: {r['finding']}" + (f" ({given})" if given else ""))
    return out


def in_scope(case: dict, cfg: dict) -> bool:
    """An open case raised under the new process, of a kind whose scan is switched on:
    an aggregator case (`auto_checks_dsp`) or one raised to engineering
    (`auto_checks_engineering`). An engineering case with its own aggregator case is left
    to that one while the aggregator scan is on, so a check runs once per problem."""
    start, raised = parse_dt(cfg.get("sla_start")), parse_dt(case.get("created_at"))
    if raised is None or (start is not None and raised < start):
        return False
    dsp_on = cfg.get("auto_checks_dsp", True)
    if case.get("record_type") == cfg.get("dependency_record_type"):
        return bool(dsp_on)
    return bool(cfg.get("auto_checks_engineering")) and is_engineering(case) and not (dsp_on and _has_dsp(case, cfg))


def inputs(case: dict, evidence: list[dict], cfg: dict) -> dict[str, str]:
    """Everything a check can take, from the case's fields and text."""
    first = lambda v: next((s.strip() for s in str(v or "").split(",") if s.strip()), None)  # noqa: E731
    out: dict[str, str] = {}
    names = case.get("third_parties") or []
    if names:
        out["platform"] = str((cfg.get("platform_aliases") or {}).get(names[0]) or names[0]).lower()
    for key in ("location_id", "biz_id"):
        if (value := first(case.get(key))):
            out[key] = value
    raised = parse_dt(case.get("created_at"))
    if raised:
        utc = raised.astimezone(timezone.utc)
        out["case_date"] = utc.strftime("%Y-%m-%d")
        out["from"] = (utc - timedelta(days=WINDOW_BEFORE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    if (orders := case_details.order_ids(case, evidence)):
        out["order_id"] = orders[0]
    return out


def _saved(check: dict, question: dict) -> bool:
    card = str(question.get("card_id") or "")
    if card and str(check.get("card_id") or "") == card:
        return True
    return bool(question.get("name")) and (check.get("check_name") or "").strip() == question["name"]


def _latest(checks: list[dict], question: dict) -> dict | None:
    found = [c for c in checks if _saved(c, question)]
    return max(found, key=lambda c: c.get("created_at") or "") if found else None


def plan(case: dict, evidence: list[dict], checks: list[dict], cfg: dict) -> dict:
    """{"due": [...], "blocked": [...], "done": [...]} for one case. A case with no rule gets nothing."""
    rule = rule_for(case, cfg.get("auto_checks") or [])
    questions = cfg.get("check_questions") or {}
    out: dict[str, list] = {"due": [], "blocked": [], "done": []}
    if not rule:
        return out
    known = inputs(case, evidence, cfg)
    for kind in rule.get("checks") or []:
        spec, question = KINDS.get(kind), questions.get(kind)
        if not spec or not question:
            continue
        name = question.get("name") or kind
        if (saved := _latest(checks, question)):
            out["done"].append({"kind": kind, "name": name, "row_count": saved.get("row_count")})
            continue
        missing = [LABELS.get(n, n) for n in spec.get("needs", []) if n not in known]
        if missing:
            out["blocked"].append({"kind": kind, "name": name, "reason": f"Needs {', '.join(missing)}"})
            continue
        if (after := spec.get("after")):
            first = _latest(checks, questions.get(after) or {})
            after_name = (questions.get(after) or {}).get("name") or after
            if first is None:
                out["blocked"].append({"kind": kind, "name": name, "reason": f"Waits for {after_name}"})
                continue
            if not first.get("row_count"):
                out["blocked"].append({"kind": kind, "name": name, "reason": f"{after_name} found nothing"})
                continue
        params = {n: known[n] for n in [*spec.get("needs", []), *spec.get("optional", [])] if n in known}
        item = {"kind": kind, "name": name, "card_id": question.get("card_id"), "params": params}
        if spec.get("fill"):
            item["fill_from"] = {"check": (questions.get(spec["after"]) or {}).get("name") or spec["after"],
                                 "params": spec["fill"]}
        out["due"].append(item)
    return out


def due(items: list[tuple], checks_by_case: dict[str, list[dict]], cfg: dict,
        limit: int = DUE_LIMIT) -> dict:
    """The routine's work list, newest case first. items: (case, verdict, evidence)."""
    cases, blocked = [], []
    for case, _, evidence in sorted(items, key=lambda i: i[0].get("created_at") or "", reverse=True):
        if not in_scope(case, cfg):
            continue
        p = plan(case, evidence, checks_by_case.get(case["id"]) or [], cfg)
        head = {"case_number": case.get("case_number"), "case_id": case["id"], "subject": case.get("subject")}
        if p["due"]:
            cases.append({**head, "checks": p["due"]})
        blocked += [{**head, **b} for b in p["blocked"]]
    return {"total": sum(len(c["checks"]) for c in cases), "cases": cases[:limit],
            "more": max(len(cases) - limit, 0), "blocked": blocked}


def due_rows(tenant_id: str, items: list[tuple], checks_by_case: dict[str, list[dict]], cfg: dict,
             computed_at: str) -> list[dict]:
    """Every due check as a monitor_checks_due row."""
    listed = due(items, checks_by_case, cfg, limit=len(items) or 1)["cases"]
    return [{"tenant_id": tenant_id, "case_id": c["case_id"], "kind": x["kind"], "case_number": c["case_number"],
             "check_name": x["name"], "card_id": None if x.get("card_id") in (None, "") else str(x["card_id"]),
             "params": x["params"], "fill_from": x.get("fill_from"), "computed_at": computed_at}
            for c in listed for x in c["checks"]]


def found_nothing(items: list[tuple], checks_by_case: dict[str, list[dict]], cfg: dict) -> list[dict]:
    """Automatic checks that returned no rows, newest first: each is something to tell the agent."""
    questions = cfg.get("check_questions") or {}
    rows = []
    for case, _, evidence in items:
        if not in_scope(case, cfg):
            continue
        rule = rule_for(case, cfg.get("auto_checks") or [])
        for kind in (rule or {}).get("checks") or []:
            question = questions.get(kind)
            saved = _latest(checks_by_case.get(case["id"]) or [], question or {}) if question else None
            if saved is None or saved.get("row_count"):
                continue
            rows.append({"case_id": case["id"], "case_number": case.get("case_number"),
                         "owner": case.get("owner") or "Unassigned", "subject": case.get("subject"),
                         "check": question.get("name") or kind, "finding": KINDS[kind]["empty"],
                         "params": saved.get("params") or {}, "summary": saved.get("summary"),
                         "at": saved.get("created_at")})
    rows.sort(key=lambda r: r["at"] or "", reverse=True)
    return rows


def card(rows: list[dict], rank: int) -> dict | None:
    """One Needs Me card for every check that found nothing."""
    if not rows:
        return None
    n, cases = len(rows), len({r["case_id"] for r in rows})
    lines = ["Hi team, our data checks found nothing for these cases. Please confirm the details "
             "with the merchant before chasing the aggregator:", ""]
    for r in rows:
        given = _given(r)
        lines.append(f"- {r['case_number']} ({r['owner']}): {r['finding']}" + (f" ({given})" if given else ""))
    return {"key": "checks:found-nothing", "rank": rank, "kind": "checks",
            "title": f"{n} data check{'' if n == 1 else 's'} found nothing",
            "why": f"{rows[0]['check']}: {rows[0]['finding']}. First: {rows[0]['subject']}",
            "chip": f"{cases} case{'' if cases == 1 else 's'}", "tone": "warn",
            "draft": "\n".join(lines), "draft_label": "Message for the agents",
            "activity_at": max(r["at"] or "" for r in rows) or None,
            "case_ids": list(dict.fromkeys(r["case_id"] for r in rows)),
            "steps": {r["case_id"]: r["finding"] for r in rows}}


def validate(cfg: dict) -> list[str]:
    errors = []
    questions = cfg.get("check_questions", {})
    if not isinstance(questions, dict):
        return ["check_questions must map a check kind to its saved question"]
    for kind, q in questions.items():
        if kind not in KINDS:
            errors.append(f"check_questions: unknown check kind {kind!r}; one of {', '.join(KINDS)}")
        elif not (isinstance(q, dict) and (q.get("card_id") or q.get("name"))):
            errors.append(f"check_questions.{kind} needs a card_id or name")
    for rule in cfg.get("auto_checks", []):
        if not isinstance(rule, dict) or not rule.get("checks"):
            errors.append("each auto_checks entry needs checks")
            continue
        errors += [f"auto_checks: unknown check kind {k!r}" for k in rule["checks"] if k not in KINDS]
        errors += [f"auto_checks: {k} has to come after {KINDS[k]['after']}" for k in rule["checks"]
                   if k in KINDS and KINDS[k].get("after") and KINDS[k]["after"] not in rule["checks"]]
    return errors
