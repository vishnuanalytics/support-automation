"""
Bootstrap `case_memory` from a tenant's historical cases — a Salesforce
Bulk-API XML export (EmailMessage + optional Case), or a flat CSV.

Used by the "import case history" action in Setup / Knowledge (so a new
tenant gets useful `case_lookup` answers from day one) and by
`scripts/import_gunner_cases.py`.

    rows, stats = parse_salesforce_bulk(email_xml_bytes, case_xml_bytes, tenant_id=tid)
    rows, stats = parse_flat_csv(csv_bytes, tenant_id=tid)
    # -> feed rows to ingestion.case_memory_sync._sync_rows(rows, dry=False)

Per Case: first inbound message = the problem, the substantive UrbanPiper
outbound (direct, or mined from quoted history) = the resolution. Closing-
boilerplate-only and customer-only threads are dropped.
"""

from __future__ import annotations

import csv as _csv
import io
import re
import xml.etree.ElementTree as ET

from interpreter import case_memory

_NS = "{http://www.force.com/2009/06/asyncapi/dataload}"

_QUOTE_MARKERS = [
    re.compile(r"-{3,}\s*Original Message\s*-{3,}", re.I),
    re.compile(r"\n_{5,}\n"),
    re.compile(r"\nOn .{0,80}\bwrote:\s*\n", re.I),
    re.compile(r"\nFrom:\s.+?\nSent:\s", re.I | re.S),
    re.compile(r"\n>+ ", re.I),
]
_SIG = re.compile(
    r"(Thanks\s*&?\s*Regards.*|Should you feel that you are currently not receiving.*"
    r"|For any issues drop an email to support@\S+.*"
    r"|Greetings from .{0,40}Support!.*?\n)",
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


# --------------------------------------------------------------------------- #
# text cleaning
# --------------------------------------------------------------------------- #
def _strip_headers(text: str) -> str:
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
    if not text:
        return ""
    cut = len(text)
    for rx in _QUOTE_MARKERS:
        m = rx.search(text)
        if m:
            cut = min(cut, m.start())
    return re.sub(r"\n{3,}", "\n\n", _strip_headers(_SIG.sub("", text[:cut]))).strip()


def _quoted_blocks(text: str) -> list[dict]:
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
        fm = re.search(r"from:\s*(.+)", head)
        wrote = re.search(r"\bon .{0,120}\bwrote:", head)
        sender = (fm.group(1) if fm else "") + (wrote.group(0) if wrote else "")
        from_up = ("urbanpiper" in sender) or (not fm and not wrote)
        if fm and "urbanpiper" not in sender:
            from_up = "urbanpiper" in head
        out.append({"text": _clean(p), "from_up": from_up})
    return out


def _ok_answer(c: str, min_len: int = 80) -> bool:
    if not c or len(c) < min_len or _BOILERPLATE.search(c):
        return False
    low = c.lower()[:60]
    return not any(p in low for p in (
        "thank you", "thanks team", "problem is fix", "issue is fixed",
        "it's working", "its working", "resolved now"))


def _resolution(msgs: list[dict], min_len: int = 80) -> str | None:
    cands: list[str] = []
    for m in msgs:
        if not m["incoming"]:
            c = _clean(m["body"])
            if _ok_answer(c, min_len):
                cands.append(c)
        for q in _quoted_blocks(m["body"]):
            if q["from_up"] and _ok_answer(q["text"], min_len):
                cands.append(q["text"])
    return max(cands, key=len)[:4000] if cands else None


def best_resolution(raw_outbound: list[str], *, min_len: int = 50) -> str | None:
    """Pick the substantive support answer from a Case's outbound email
    bodies (newest first). Strips quotes + signatures, skips closing
    boilerplate ("we are marking this resolved") and mines quoted history
    for the real prior answer. Shared by the live `case_memory_sync
    --from-salesforce` path and the bulk-export importer."""
    return _resolution([{"incoming": False, "body": b or ""} for b in raw_outbound],
                       min_len)


def _problem(msgs: list[dict], subject: str | None) -> str:
    for m in msgs:
        if m["incoming"]:
            c = _clean(m["body"])
            if c:
                return c[:2000]
    return (subject or "").strip()


# --------------------------------------------------------------------------- #
# XML parsing
# --------------------------------------------------------------------------- #
def _records(data: bytes):
    for _, el in ET.iterparse(io.BytesIO(data)):
        if el.tag == f"{_NS}records":
            row: dict[str, str] = {}
            for ch in el:
                tag = ch.tag.replace(_NS, "")
                if ch.text and ch.text.strip():
                    row.setdefault(tag, ch.text)
            el.clear()
            yield row


def sniff(data: bytes) -> str:
    head = data[:600].decode("utf-8", "replace")
    if "<type>EmailMessage</type>" in head:
        return "sf_email"
    if "<type>Case</type>" in head:
        return "sf_case"
    if head.lstrip().startswith("<?xml") or "<records" in head:
        return "xml_other"
    if "," in head.splitlines()[0] if head.splitlines() else False:
        return "csv"
    return "unknown"


def _row(m: dict | None, msgs: list[dict], tenant_id: str, pid: str) -> dict | None:
    res = _resolution(msgs)
    if not res:
        return None
    subj = (m or {}).get("subject") or (msgs[0]["subject"] if msgs else "")
    subj = re.sub(r"^(Re|Fwd|Fw):\s*", "", subj or "", flags=re.I).strip()
    return {
        "case_sf_id": (m or {}).get("case_sf_id") or f"import:{pid}",
        "tenant_id": tenant_id,
        "case_number": (m or {}).get("case_number"),
        "subject": subj or None,
        "body_summary": _problem(msgs, subj) or subj,
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
        "source": "import",
    }


def parse_salesforce_bulk(email_xml: bytes, case_xml: bytes | None, *,
                          tenant_id: str) -> tuple[list[dict], dict]:
    """(rows, stats). `case_xml` is optional — without it, threads are kept
    on the strength of a usable resolution alone (no closed-status filter,
    no type/module)."""
    by_case: dict[str, list[dict]] = {}
    for r in _records(email_xml):
        pid = (r.get("ParentId") or "")[:15]
        if not pid:
            continue
        by_case.setdefault(pid, []).append({
            "date": r.get("MessageDate", ""),
            "incoming": r.get("Incoming", "false").lower() == "true",
            "subject": r.get("Subject", ""),
            "body": r.get("TextBody", ""),
        })
    for msgs in by_case.values():
        msgs.sort(key=lambda m: m["date"])

    meta: dict[str, dict] = {}
    if case_xml:
        need = set(by_case)
        for r in _records(case_xml):
            cid = (r.get("Case_Id_18char__c") or r.get("Id") or "")[:15]
            if not cid or cid not in need:
                continue
            meta[cid] = {
                "case_sf_id": r.get("Case_Id_18char__c") or r.get("Id"),
                "case_number": r.get("CaseNumber"),
                "is_closed": r.get("IsClosed", "false").lower() == "true",
                "closed_at": r.get("ClosedDate") or r.get("Case_Close_Time__c"),
                "type": r.get("Type") or r.get("Type__c") or r.get("Type_of_Request__c"),
                "module": r.get("Module__c"),
                "submodule": r.get("Sub_Module__c"),
                "subject": r.get("Subject"),
                "account_id": r.get("AccountId"),
            }

    rows: list[dict] = []
    stats = {"threads": len(by_case), "matched_case": 0, "closed": 0,
             "dropped_open": 0, "dropped_no_resolution": 0, "kept": 0}
    for pid, msgs in by_case.items():
        m = meta.get(pid)
        if m:
            stats["matched_case"] += 1
            if not m["is_closed"]:
                stats["dropped_open"] += 1
                continue
            stats["closed"] += 1
        row = _row(m, msgs, tenant_id, pid)
        if not row:
            stats["dropped_no_resolution"] += 1
            continue
        rows.append(row)
        stats["kept"] += 1
    return rows, stats


def parse_flat_csv(data: bytes, *, tenant_id: str) -> tuple[list[dict], dict]:
    """A simple export: columns (case-insensitive) subject, problem|description,
    resolution, resolved_at|closed_at, and optional case_number, type, module."""
    text = data.decode("utf-8-sig", "replace")
    reader = _csv.DictReader(io.StringIO(text))
    lc = {(h or "").strip().lower(): h for h in (reader.fieldnames or [])}

    def col(row, *names):
        for n in names:
            if n in lc and row.get(lc[n]):
                return str(row[lc[n]]).strip()
        return ""

    rows, stats = [], {"threads": 0, "kept": 0, "dropped_no_resolution": 0}
    for r in reader:
        stats["threads"] += 1
        res = col(r, "resolution", "resolution_text", "answer")
        if len(res) < 40:
            stats["dropped_no_resolution"] += 1
            continue
        subj = col(r, "subject", "title") or None
        rows.append({
            "case_sf_id": col(r, "case_id", "case_sf_id", "id")
            or f"csv:{col(r, 'case_number') or stats['threads']}",
            "tenant_id": tenant_id,
            "case_number": col(r, "case_number", "casenumber") or None,
            "subject": subj,
            "body_summary": col(r, "problem", "description", "body", "customer_message") or subj,
            "case_type": col(r, "type", "case_type") or None,
            "module": col(r, "module") or None,
            "submodule": col(r, "submodule", "sub_module") or None,
            "region": None, "tier": None, "account_id": col(r, "account_id") or None,
            "resolution_kind": case_memory.classify_resolution_kind(None, res),
            "resolution_text": res[:4000],
            "generalizable": not case_memory.looks_specific(res),
            "agent_user_id": None,
            "resolved_at": col(r, "resolved_at", "closed_at", "closeddate") or None,
            "source": "import",
        })
        stats["kept"] += 1
    return rows, stats
