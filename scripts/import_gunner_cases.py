"""
One-off: import UrbanPiper's exported cases (Salesforce Bulk API XML) into
the gunner tenant's `case_memory` so `case_lookup` / `draft` can cite real
prior resolutions.

Inputs (in ./gunner urbanpiper old cases/):
  * an EmailMessage bulk-query result  (Id, ParentId, Subject, TextBody,
    FromAddress, ToAddress, MessageDate, Incoming)
  * a Case bulk-query result           (CaseNumber, Case_Id_18char__c,
    IsClosed, ClosedDate, Type*, Module__c, Sub_Module__c, Subject, AccountId)

Per Case: first inbound message = the problem, the substantive outbound
message = the resolution (closing-boilerplate-only threads are dropped;
quoted history inside a closing mail is mined for the real answer).

    python -m scripts.import_gunner_cases            # import
    python -m scripts.import_gunner_cases --dry-run  # parse + report, write nothing
"""

from __future__ import annotations

import argparse
import glob
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from ingestion import case_memory_sync as cms  # noqa: E402
from interpreter import case_memory  # noqa: E402

TENANT = "ee4102db-ab47-4deb-b28b-9efc5a02575b"  # gunner
DIR = pathlib.Path(__file__).resolve().parents[1] / "gunner urbanpiper old cases"
NS = "{http://www.force.com/2009/06/asyncapi/dataload}"

_QUOTE_MARKERS = [
    re.compile(r"-{3,}\s*Original Message\s*-{3,}", re.I),
    re.compile(r"\n_{5,}\n"),
    re.compile(r"\nOn .{0,80}\bwrote:\s*\n", re.I),
    re.compile(r"\nFrom:\s.+?\nSent:\s", re.I | re.S),
    re.compile(r"\n>+ ", re.I),
]
_SIG = re.compile(
    r"(Thanks\s*&?\s*Regards.*|Should you feel that you are currently not receiving.*"
    r"|For any issues drop an email to support@urbanpiper\.com.*"
    r"|Greetings from UrbanPiper Support!.*?\n)",
    re.I | re.S,
)
_BOILERPLATE = re.compile(
    r"marking this (support )?case as resolved|we are marking this|as your concern has been "
    r"addressed|thank you for confirming that the issue has been resolved|please feel free to "
    r"reply to this email to reopen|closing this (case|ticket)|no response from your end",
    re.I,
)


_HDR_LINE = re.compile(r"^(from|sent|to|cc|bcc|subject|date|reply-to|importance)\s*:.*$",
                       re.I | re.M)
_ON_WROTE = re.compile(r"^\s*On .{0,120}\bwrote:\s*$", re.I | re.M)


def _strip_headers(text: str) -> str:
    """Drop a leading run of `Header: value` lines and any `On ... wrote:`
    preamble left at the top after quote-splitting."""
    s = text
    for _ in range(6):
        s2 = _ON_WROTE.sub("", s, count=1).lstrip()
        m = _HDR_LINE.match(s2)
        while m:
            s2 = s2[m.end():].lstrip("\n")
            m = _HDR_LINE.match(s2)
        s2 = re.sub(r"^>+ ?", "", s2, flags=re.M).strip()
        if s2 == s:
            break
        s = s2
    return s


def _clean(text: str) -> str:
    """Top (non-quoted) portion of a message, headers + signature stripped."""
    if not text:
        return ""
    cut = len(text)
    for rx in _QUOTE_MARKERS:
        m = rx.search(text)
        if m:
            cut = min(cut, m.start())
    top = _SIG.sub("", text[:cut])
    top = _strip_headers(top)
    return re.sub(r"\n{3,}", "\n\n", top).strip()


def _quoted_blocks(text: str) -> list[dict]:
    """Split a mail into its quoted prior messages. Each: {text, from_up}."""
    if not text:
        return []
    parts = re.split(
        r"-{3,}\s*Original Message\s*-{3,}|\n_{5,}\n|(?=\nOn .{0,120}\bwrote:\s*\n)"
        r"|(?=\nFrom:\s.+?\nSent:\s)",
        text, flags=re.S)
    out = []
    for p in parts[1:]:
        if not p or not p.strip():
            continue
        head = p[:200].lower()
        # who sent this quoted message?
        fm = re.search(r"from:\s*(.+)", head)
        wrote = re.search(r"\bon .{0,120}\bwrote:", head)
        sender = (fm.group(1) if fm else "") + (wrote.group(0) if wrote else "")
        from_up = ("urbanpiper" in sender) or (not fm and not wrote)
        if fm and "urbanpiper" not in sender and "urbanpiper" not in head:
            from_up = "urbanpiper" in head
        out.append({"text": _clean(p), "from_up": from_up})
    return out


def _txt(el) -> dict:
    out: dict[str, str] = {}
    for ch in el:
        tag = ch.tag.replace(NS, "")
        if ch.text and ch.text.strip():
            out.setdefault(tag, ch.text)
    return out


def _emails() -> dict[str, list[dict]]:
    path = next(iter(sorted(DIR.glob("*.xml"))), None)
    by_case: dict[str, list[dict]] = {}
    files = [p for p in DIR.glob("*.xml")]
    for f in files:
        # cheap sniff: is this the EmailMessage file?
        head = f.read_bytes()[:400].decode("utf-8", "replace")
        if "<type>EmailMessage</type>" not in head:
            continue
        for _, el in ET.iterparse(str(f)):
            if el.tag != f"{NS}records":
                continue
            r = _txt(el)
            el.clear()
            pid = (r.get("ParentId") or "")[:15]
            if not pid:
                continue
            by_case.setdefault(pid, []).append({
                "date": r.get("MessageDate", ""),
                "incoming": (r.get("Incoming", "false").lower() == "true"),
                "subject": r.get("Subject", ""),
                "body": r.get("TextBody", ""),
                "from": r.get("FromAddress", ""),
            })
    for msgs in by_case.values():
        msgs.sort(key=lambda m: m["date"])
    return by_case


def _cases(needed: set[str]) -> dict[str, dict]:
    meta: dict[str, dict] = {}
    for f in DIR.glob("*.xml"):
        head = f.read_bytes()[:400].decode("utf-8", "replace")
        if "<type>Case</type>" not in head:
            continue
        for _, el in ET.iterparse(str(f)):
            if el.tag != f"{NS}records":
                continue
            r = _txt(el)
            el.clear()
            cid = (r.get("Case_Id_18char__c") or "")[:15]
            if not cid or cid not in needed:
                continue
            meta[cid] = {
                "case_sf_id": r.get("Case_Id_18char__c"),
                "case_number": r.get("CaseNumber"),
                "is_closed": (r.get("IsClosed", "false").lower() == "true"),
                "closed_at": r.get("ClosedDate") or r.get("Case_Close_Time__c"),
                "type": r.get("Type") or r.get("Type__c") or r.get("Type_of_Request__c"),
                "module": r.get("Module__c"),
                "submodule": r.get("Sub_Module__c"),
                "subject": r.get("Subject"),
                "account_id": r.get("AccountId"),
            }
    return meta


def _ok_answer(c: str) -> bool:
    if not c or len(c) < 80:
        return False
    if _BOILERPLATE.search(c):
        return False
    # reject a block that's just a customer one-liner / thank-you
    low = c.lower()
    if any(p in low[:60] for p in ("thank you", "thanks team", "problem is fix", "issue is fixed",
                                   "it's working", "its working", "resolved now")):
        return False
    return True


def _resolution(msgs: list[dict]) -> str | None:
    """Best substantive UrbanPiper answer for the thread."""
    cands: list[str] = []
    for m in msgs:
        if not m["incoming"]:
            c = _clean(m["body"])
            if _ok_answer(c):
                cands.append(c)
        # mine quoted history (in any message) for a prior UrbanPiper answer
        for q in _quoted_blocks(m["body"]):
            if q["from_up"] and _ok_answer(q["text"]):
                cands.append(q["text"])
    if not cands:
        return None
    # the longest is usually the substantive troubleshooting message
    return max(cands, key=len)[:4000]


def _problem(msgs: list[dict], subject: str | None) -> str:
    for m in msgs:
        if m["incoming"]:
            c = _clean(m["body"])
            if c:
                return c[:2000]
    return (subject or "").strip()


def build_rows() -> tuple[list[dict], dict]:
    by_case = _emails()
    meta = _cases(set(by_case))
    rows, stats = [], {"threads": len(by_case), "matched_case": 0, "closed": 0,
                       "no_resolution": 0, "kept": 0}
    for pid, msgs in by_case.items():
        m = meta.get(pid)
        if m:
            stats["matched_case"] += 1
        if m and not m["is_closed"]:
            continue
        if m and m["is_closed"]:
            stats["closed"] += 1
        res = _resolution(msgs)
        if not res:
            stats["no_resolution"] += 1
            continue
        subj = (m or {}).get("subject") or (msgs[0]["subject"] if msgs else "")
        prob = _problem(msgs, subj)
        rows.append({
            "case_sf_id": (m or {}).get("case_sf_id") or f"import:{pid}",
            "tenant_id": TENANT,
            "case_number": (m or {}).get("case_number"),
            "subject": re.sub(r"^(Re|Fwd):\s*", "", subj or "", flags=re.I).strip() or None,
            "body_summary": prob or subj,
            "case_type": (m or {}).get("type"),
            "module": (m or {}).get("module"),
            "submodule": (m or {}).get("submodule"),
            "region": None,
            "tier": None,
            "account_id": (m or {}).get("account_id"),
            "resolution_kind": case_memory.classify_resolution_kind(None, res),
            "resolution_text": res,
            "generalizable": not case_memory.looks_specific(res),
            "agent_user_id": None,
            "resolved_at": (m or {}).get("closed_at") or (msgs[-1]["date"] if msgs else None),
            "source": "salesforce-import",
        })
        stats["kept"] += 1
    return rows, stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows, stats = build_rows()
    print(f"threads (distinct Case parents in email export): {stats['threads']}")
    print(f"  matched to a Case row : {stats['matched_case']}")
    print(f"  closed cases          : {stats['closed']}")
    print(f"  dropped (no usable resolution): {stats['no_resolution']}")
    print(f"  -> case_memory rows    : {stats['kept']}")
    if not rows:
        print("nothing to import")
        return
    for r in rows[:5]:
        print(f"\n  [{r['case_number']}] {r['subject']}")
        print(f"    problem   : {(r['body_summary'] or '')[:120]!r}")
        print(f"    resolution: {(r['resolution_text'] or '')[:160]!r}")
        print(f"    kind={r['resolution_kind']} generalizable={r['generalizable']}")

    n = cms._sync_rows(rows, dry=args.dry_run)
    print(f"\n{'[dry-run] would sync' if args.dry_run else 'synced'} {n} case_memory row(s) "
          f"for tenant {TENANT}")


if __name__ == "__main__":
    main()
