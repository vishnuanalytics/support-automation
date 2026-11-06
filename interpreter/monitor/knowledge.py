"""Past resolved cases as a searchable Knowledge collection, and AI drafts from them.

Each resolved case becomes one Knowledge entry holding its whole conversation,
with personal details redacted. Quoted reply chains are removed first, so a
thread's text appears once. No LLM is involved in building the collection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from typing import Any

from . import verdict
from .redact import clean, redact_pii

log = logging.getLogger("interpreter.monitor.knowledge")

COLLECTION = "Resolved third-party cases"
MESSAGE_LIMIT = 4000
_ENTRY_NS = uuid.UUID("7f1a9d3e-52b1-4c7a-9b5e-3d2c4a6f8e10")
_QUOTE = re.compile(r"(^\s*On .{5,120} wrote:\s*$|^-{2,}\s*Original Message|^-{2,}\s*Forwarded message|"
                    r"^From:\s.+$\n^(Sent|Date):\s)", re.M | re.I)


def strip_quoted(text: str | None) -> str:
    """Keep only what this email added, dropping the quoted thread below it."""
    body = text or ""
    match = _QUOTE.search(body)
    if match and match.start() > 0:
        body = body[:match.start()]
    return "\n".join(line for line in body.splitlines() if not line.startswith(">")).strip()


def message_text(text: str | None) -> str:
    return redact_pii(clean(strip_quoted(text), MESSAGE_LIMIT))


def entry_id(case_id: str) -> str:
    return str(uuid.uuid5(_ENTRY_NS, case_id))


def conversation_md(case: dict, cfg: dict) -> str:
    """One case's full history as markdown, ready for embedding."""
    third = cfg.get("third_party_label", "Third party")
    labels = {verdict.US: "Agent", verdict.THIRD_PARTY: third,
              verdict.CUSTOMER: cfg.get("customer_label", "Customer")}
    facts = [("Aggregator", ", ".join(case.get("third_parties") or [])), ("Module", case.get("module")),
             ("Sub-module", case.get("sub_module")), ("BU", case.get("bu")),
             ("Resolved", (case.get("closed_at") or "")[:10])]
    lines = [f"# {redact_pii(case.get('subject')) or 'Case'}", ""]
    lines += [f"- {k}: {v}" for k, v in facts if v]
    if case.get("description"):
        lines += ["", "## Problem", message_text(case["description"])]
    lines += ["", "## Conversation"]
    for m in sorted(case.get("messages") or [], key=lambda x: x.get("at") or ""):
        party = verdict.email_party(m.get("from"), bool(m.get("incoming")), case, cfg)
        body = message_text(m.get("text"))
        if body:
            lines += [f"### {labels[party]} ({(m.get('at') or '')[:10]})", body, ""]
    if case.get("comments"):
        lines += ["## Internal notes"]
        lines += [f"- {message_text(c)}" for c in case["comments"] if message_text(c)]
    return "\n".join(lines)


def ensure_collection(sb: Any, tenant_id: str) -> str:
    rows = (sb.table("sources").select("source_id").eq("tenant_id", tenant_id)
            .eq("name", COLLECTION).execute().data or [])
    if rows:
        return rows[0]["source_id"]
    return sb.table("sources").insert({
        "kind": "internal_kb", "tenant_id": tenant_id, "name": COLLECTION,
        "config": {"description": "Resolved cases with their full conversations."},
    }).execute().data[0]["source_id"]


def save_case(sb: Any, tenant_id: str, source_id: str, case: dict, cfg: dict) -> bool:
    """Create or refresh one entry. Returns False when nothing has changed."""
    from datetime import datetime, timezone

    from ingestion.sources.kb_common import embed_entry

    body = conversation_md(case, cfg)
    eid = entry_id(case["id"])
    digest = hashlib.md5(body.encode()).hexdigest()  # noqa: S324 -- same change check as the KB
    old = sb.table("kb_entries").select("embed_hash").eq("entry_id", eid).execute().data or []
    if old and old[0].get("embed_hash") == digest:
        return False
    title = f"{case.get('case_number')}: {redact_pii(case.get('subject'))}"
    sb.table("kb_entries").upsert({
        "entry_id": eid, "source_id": source_id, "tenant_id": tenant_id, "title": title[:200],
        "body_md": body, "status": "active"}).execute()
    chunks = embed_entry(sb, source_id=source_id, url=f"kb://{source_id}/{eid}", title=title,
                         body_md=body, section=COLLECTION)
    sb.table("kb_entries").update({
        "embed_hash": digest, "chunk_count": chunks,
        "embedded_at": datetime.now(timezone.utc).isoformat()}).eq("entry_id", eid).execute()
    return True


def backfill(sb: Any, tenant_id: str, reader: Any, partners: list[dict], cfg: dict,
             since: str, until: str, *, progress: Any = None) -> dict[str, int]:
    index = {n.lower(): p for p in partners for n in [p["name"], *(p.get("aliases") or [])]}
    source_id = ensure_collection(sb, tenant_id)
    counts = {"cases": 0, "saved": 0, "failed": 0}
    for case in reader.resolved_conversations(cfg["dependency_record_type"], since, until):
        counts["cases"] += 1
        case["partner"] = next((index[n.lower()] for n in case.get("third_parties") or []
                                if n.lower() in index), None)
        try:
            counts["saved"] += save_case(sb, tenant_id, source_id, case, cfg)
        except Exception as e:  # noqa: BLE001 -- one bad case must not stop the rest
            counts["failed"] += 1
            log.warning("knowledge save failed: %s", type(e).__name__)
        if progress and counts["cases"] % 50 == 0:
            progress(counts)
    return counts


def similar_context(tenant_id: str, query: str, sb: Any, top_k: int = 4) -> list[dict]:
    from interpreter.retrieval import hybrid_retrieve

    results, _ = hybrid_retrieve(query[:800], top_k=top_k, use_graph=False,
                                 kb_sources=[COLLECTION], tenant_id=tenant_id, sb=sb)
    return [{"title": r.get("title") or r.get("heading_path") or COLLECTION,
             "text": r.get("chunk_text") or r.get("text") or "",
             "url": r.get("doc_url") or r.get("url")} for r in results]


DRAFT_SYSTEM = (
    "You help a restaurant software company's support lead. Using only the numbered sources, "
    "write a reply for the requested audience. Past cases show how similar issues went: use them "
    "for approach, but never state their details as facts about this case. Never promise an ETA "
    "or an RCA unless a source commits to one. Keep any placeholder like [email] or [number] "
    "exactly as written. Cite sources only in situation, never in the draft. If a needed fact is "
    "missing, list it under missing. Answer as JSON: "
    "{\"situation\": two or three sentences on where this case stands, citing sources like [1], "
    "\"draft\": the message, \"missing\": [facts to fill in before sending]}."
)


def draft_prompt(case: dict, audience: str, timeline: list[dict], similar: list[dict],
                 partner: dict | None) -> tuple[str, list[dict]]:
    sources = [{"source": t["source"], "url": t.get("url"),
                "text": f"{t['party_label']} ({(t.get('at') or '')[:10]}): "
                        f"{redact_pii(t.get('text'))}"} for t in timeline[-20:] if t.get("text")]
    sources += [{"source": "Past case", "url": s.get("url"), "text": redact_pii(s["text"])}
                for s in similar]
    numbered = "\n\n".join(f"[{i + 1}] {s['source']}: {s['text']}" for i, s in enumerate(sources))
    target = "the merchant" if audience == "merchant" else (
        f"the {partner['name']} team" if partner else "the delivery platform's team")
    header = (f"Audience: {target}\nCase: {redact_pii(case.get('subject'))}\n"
              f"Aggregator: {', '.join(case.get('third_parties') or []) or 'not stated'}\n")
    return f"{header}\nSources:\n{numbered}", sources


def generate_draft(tenant_id: str, case: dict, audience: str, timeline: list[dict],
                   sb: Any, partner: dict | None, complete: Any = None) -> dict[str, Any]:
    from interpreter import llm

    query = " ".join([case.get("subject") or "", *[t.get("text") or "" for t in timeline[-3:]]])
    similar = similar_context(tenant_id, redact_pii(query), sb)
    user, sources = draft_prompt(case, audience, timeline, similar, partner)
    raw = (complete or llm.complete)(DRAFT_SYSTEM, user, max_tokens=900, json_object=True,
                                     tenant_id=tenant_id)
    body = json.loads(raw)
    draft = re.sub(r" ?\[\d+\](?:\s*\[\d+\])*", "", body.get("draft") or "")
    return {"situation": body.get("situation") or "", "draft": draft,
            "missing": body.get("missing") or [],
            "sources": [{"n": i + 1, "source": s["source"], "url": s.get("url")}
                        for i, s in enumerate(sources)],
            "similar_used": len(similar)}
