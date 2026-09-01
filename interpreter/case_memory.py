"""
Phase 21 — Case-resolution memory.

Remembers how past Cases were resolved and surfaces the closest ones so the
`draft` node can answer from real resolutions, not just KB docs.

    interpreter/case_memory.py   this module — embed / redact / classify /
                                 upsert / graph-sync / lookup
    ingestion/case_memory_sync.py  the periodic populator
    db/migrations/048_case_memory.sql  the pgvector table + match_case_memory

Two hard rules (the "pattern vs proof" boundary):
  * A resolution is `generalizable` only when its text is a reusable pattern
    (a how-to, a known fix). One that cites this customer's own data (IDs,
    timestamps, log lines) is NOT — it may seed an *investigation hint*, never
    reply copy.
  * `answer_mode == 'diagnostic'` callers get hints only; `draft` must not
    state a specific factual claim that came from memory.

Everything degrades: no Supabase rows / no Neo4j / no embedder -> empty
result, callers behave exactly as before.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("interpreter.case_memory")
# the driver logs a WARNING notification the first time we ask for a rel type
# that doesn't exist yet (DUPLICATE_OF before anything creates one) — harmless.
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

# ── redaction / "is this customer-specific?" ────────────────────────────────
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_LONGNUM_RE = re.compile(r"\b\d{5,}\b")                       # order / invoice / account ids
_ISO_TS_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_MONEY_RE = re.compile(r"[$€£]\s?\d[\d,]*(?:\.\d{2})?")
_URLQ_RE = re.compile(r"https?://\S+\?\S+")
_SF_ID_RE = re.compile(r"\b[a-zA-Z0-9]{15,18}\b")

_SPECIFIC_PATTERNS = (_UUID_RE, _LONGNUM_RE, _ISO_TS_RE, _IP_RE, _MONEY_RE, _URLQ_RE)


def looks_specific(text: str | None) -> bool:
    """True when the text leans on this customer's own data — so it is a
    diagnostic finding, not a reusable pattern."""
    if not text:
        return False
    hits = sum(1 for rx in _SPECIFIC_PATTERNS if rx.search(text))
    # an email or an SF id alone isn't enough (a pattern reply may name a
    # field); two independent specifics, or a timestamp + id, is.
    if _ISO_TS_RE.search(text) and (_LONGNUM_RE.search(text) or _UUID_RE.search(text)):
        return True
    return hits >= 2


def redact(text: str | None, *, limit: int = 900) -> str:
    """Mask customer identifiers so a stored resolution reads as a pattern."""
    if not text:
        return ""
    t = _EMAIL_RE.sub("<email>", text)
    t = _UUID_RE.sub("<id>", t)
    t = _SF_ID_RE.sub(lambda m: m.group(0) if len(m.group(0)) < 15 else "<id>", t)
    t = _LONGNUM_RE.sub("<num>", t)
    t = _IP_RE.sub("<ip>", t)
    t = _ISO_TS_RE.sub("<timestamp>", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:limit]


def summarize(text: str | None, *, limit: int = 240) -> str:
    """Cheap extractive summary — the first sentence(s) up to `limit`. No LLM."""
    t = redact(text, limit=limit * 3)
    if len(t) <= limit:
        return t
    cut = t[:limit]
    dot = cut.rfind(". ")
    return (cut[: dot + 1] if dot > 60 else cut).strip() + " …"


_CITABLE_KINDS = {"auto_reply_accepted", "agent_reply", "agent_edit_of_bot",
                  "workaround", "known_issue"}


def classify_resolution_kind(human_action: str | None, reply_text: str | None,
                             *, from_bot: bool = False) -> str:
    ha = (human_action or "").lower()
    if ha in ("sent", "sent_as_is") and from_bot:
        return "auto_reply_accepted"
    if ha in ("edited", "edit"):
        return "agent_edit_of_bot"
    if ha in ("no_reply", "none", "") and not reply_text:
        return "no_fix"
    if looks_specific(reply_text):
        return "diagnostic_finding"
    low = (reply_text or "").lower()
    if "known issue" in low or "we're aware" in low or "we are aware" in low:
        return "known_issue"
    if "workaround" in low or "in the meantime" in low:
        return "workaround"
    return "agent_reply"


# ── embedding (reuse the shared bge-small model) ────────────────────────────
def embed(text: str) -> list[float]:
    from interpreter.retrieval import embed_query

    return embed_query(text or "")


# ── Neo4j (best-effort relationship layer) ─────────────────────────────────
def _driver_or_none():
    if not os.environ.get("NEO4J_URI"):
        return None
    try:
        from ingestion.neo4j_sync import get_neo4j_driver

        return get_neo4j_driver()
    except Exception as e:  # noqa: BLE001
        log.warning("case_memory: neo4j driver unavailable: %s", e)
        return None


_MERGE_CYPHER = """
MERGE (c:Case {sf_id: $sf_id})
  SET c.case_number = $case_number, c.subject = $subject,
      c.resolution_kind = $resolution_kind, c.generalizable = $generalizable,
      c.resolved_at = $resolved_at, c.tenant_id = $tenant_id, c.status = 'active'
MERGE (r:Reply {case_sf_id: $sf_id})
  SET r.text = $resolution_text, r.accepted = $accepted, r.source = $source
MERGE (c)-[:RESOLVED_BY]->(r)
FOREACH (_ IN CASE WHEN $module   IS NULL THEN [] ELSE [1] END |
  MERGE (m:Module {name: $module})     MERGE (c)-[:ABOUT]->(m))
FOREACH (_ IN CASE WHEN $case_type IS NULL THEN [] ELSE [1] END |
  MERGE (t:CaseType {name: $case_type}) MERGE (c)-[:OF_TYPE]->(t))
FOREACH (_ IN CASE WHEN $agent    IS NULL THEN [] ELSE [1] END |
  MERGE (a:Agent {sf_user_id: $agent}) MERGE (c)-[:HANDLED_BY]->(a))
WITH c
UNWIND $similar AS sim
  MATCH (o:Case {sf_id: sim.sf_id})
  MERGE (c)-[s:SIMILAR_TO]->(o) SET s.score = sim.score
  MERGE (o)-[s2:SIMILAR_TO]->(c) SET s2.score = sim.score
  // audit NEO-3 — a near-identical case on the same account is a duplicate;
  // the older one is the canonical resolution. This makes the DUPLICATE_OF
  // boost in lookup() actually fire.
  FOREACH (_ IN CASE WHEN sim.score >= $dup_threshold
                       AND sim.same_account = true
                       AND coalesce(o.resolved_at,'') < coalesce(c.resolved_at,'~')
                     THEN [1] ELSE [] END |
    MERGE (c)-[:DUPLICATE_OF]->(o))
"""


def sync_graph(row: dict[str, Any], similar: list[dict] | None = None) -> bool:
    """MERGE one resolved Case + its edges into Neo4j. Returns False (never
    raises) when the graph isn't reachable."""
    driver = _driver_or_none()
    if driver is None:
        return False
    db = os.environ.get("NEO4J_DATABASE", "neo4j")
    try:
        driver.execute_query(
            _MERGE_CYPHER,
            sf_id=row["case_sf_id"], case_number=row.get("case_number"),
            subject=row.get("subject"), resolution_kind=row.get("resolution_kind"),
            generalizable=bool(row.get("generalizable", True)),
            resolved_at=row.get("resolved_at"),
            tenant_id=str(row.get("tenant_id") or ""),
            resolution_text=row.get("resolution_text"),
            accepted=row.get("resolution_kind") in _CITABLE_KINDS,
            source=row.get("source") or "sync",
            module=row.get("module"), case_type=row.get("case_type"),
            agent=row.get("agent_user_id"),
            dup_threshold=float(os.environ.get("CASE_MEMORY_DUP_THRESHOLD", "0.92")),
            similar=[{"sf_id": s["case_sf_id"],
                      "score": float(s.get("similarity", 0)),
                      "same_account": bool(s.get("same_account"))}
                     for s in (similar or [])],
            database_=db,
        )
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("case_memory.sync_graph(%s): %s", row.get("case_sf_id"), e)
        return False
    finally:
        try:
            driver.close()
        except Exception:  # noqa: BLE001
            pass


def _graph_duplicates(sf_ids: list[str], *, tenant_id: str | None = None) -> set[str]:
    """Which of `sf_ids` are the DUPLICATE_OF target of some other Case *in the
    same tenant* — their resolution should weigh much more. Best-effort.

    Audit NEO-5: the graph is a single shared store, so both ends of the
    traversal are pinned to `tenant_id` — a DUPLICATE_OF edge from another
    tenant's Case must never boost this tenant's ranking.
    """
    if not sf_ids:
        return set()
    driver = _driver_or_none()
    if driver is None:
        return set()
    db = os.environ.get("NEO4J_DATABASE", "neo4j")
    tid = str(tenant_id) if tenant_id else None
    try:
        recs = driver.execute_query(
            "MATCH (src:Case)-[:DUPLICATE_OF]->(o:Case) WHERE o.sf_id IN $ids "
            "AND ($tid IS NULL OR (o.tenant_id = $tid AND src.tenant_id = $tid)) "
            "RETURN DISTINCT o.sf_id AS sf_id",
            ids=sf_ids, tid=tid, database_=db,
        ).records
        return {r["sf_id"] for r in recs}
    except Exception as e:  # noqa: BLE001
        log.warning("case_memory._graph_duplicates: %s", e)
        return set()
    finally:
        try:
            driver.close()
        except Exception:  # noqa: BLE001
            pass


# ── write / read ──────────────────────────────────────────────────────────
def upsert(sb, row: dict[str, Any]) -> None:
    payload = {
        "case_sf_id": row["case_sf_id"],
        "tenant_id": str(row["tenant_id"]),
        "case_number": row.get("case_number"),
        "subject": (row.get("subject") or "")[:500] or None,
        "body_summary": summarize(row.get("body_summary")),
        "case_type": row.get("case_type"),
        "module": row.get("module"),
        "submodule": row.get("submodule"),
        "region": row.get("region"),
        "tier": row.get("tier"),
        "resolution_kind": row.get("resolution_kind", "agent_reply"),
        "resolution_text": redact(row.get("resolution_text")),
        "generalizable": bool(row.get("generalizable", True)),
        "agent_user_id": row.get("agent_user_id"),
        "resolved_at": row.get("resolved_at"),
        "embedding": row.get("embedding"),
        "status": row.get("status", "active"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    sb.table("case_memory").upsert(payload, on_conflict="case_sf_id").execute()


def _recency_factor(resolved_at: str | None) -> float:
    if not resolved_at:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(resolved_at).replace("Z", "+00:00"))
        days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
        return 1.0 / (1.0 + days / 30.0)
    except Exception:  # noqa: BLE001
        return 0.0


def lookup(
    sb,
    query_text: str,
    *,
    tenant_id: str,
    case_type: str | None = None,
    module: str | None = None,
    tier: str | None = None,
    k: int = 3,
    pool: int = 10,
    min_similarity: float = 0.35,
    use_graph: bool = True,
) -> dict[str, Any]:
    """Return `{citable: [...], hints: [...], scanned: n}`.

    * `citable`  — up to `k` generalisable resolutions the draft may quote.
    * `hints`    — one-line "past cause / thing to check" strings (from
                   non-generalisable or lower-ranked matches) — investigation
                   leads only, never reply copy.
    """
    empty = {"citable": [], "hints": [], "scanned": 0}
    if not (query_text and tenant_id):
        return empty
    try:
        vec = embed(query_text)
        rows = sb.rpc("match_case_memory", {
            "query_embedding": vec, "p_tenant": str(tenant_id), "match_count": pool,
        }).execute().data or []
    except Exception as e:  # noqa: BLE001
        log.warning("case_memory.lookup: %s", e)
        return empty
    if not rows:
        return empty

    dups = (_graph_duplicates([r["case_sf_id"] for r in rows], tenant_id=tenant_id)
            if use_graph else set())

    scored = []
    for r in rows:
        sim = float(r.get("similarity") or 0)
        if sim < min_similarity:
            continue
        rel = sim
        if case_type and r.get("case_type") == case_type:
            rel += 0.15
        if module and r.get("module") == module:
            rel += 0.10
        if tier and r.get("tier") == tier:
            rel += 0.05
        rel += 0.10 * _recency_factor(r.get("resolved_at"))
        if r["case_sf_id"] in dups:
            rel += 0.30
        r["_relevance"] = round(rel, 4)
        r["_duplicate"] = r["case_sf_id"] in dups
        scored.append(r)

    scored.sort(key=lambda x: x["_relevance"], reverse=True)

    citable, hints = [], []
    for r in scored:
        citable_ok = (r.get("generalizable")
                      and r.get("resolution_kind") in _CITABLE_KINDS
                      and r.get("resolution_text"))
        if citable_ok and len(citable) < k:
            citable.append({
                "case_number": r.get("case_number"),
                "subject": r.get("subject"),
                "resolution_text": r["resolution_text"],
                "kind": r.get("resolution_kind"),
                "duplicate": r["_duplicate"],
                "relevance": r["_relevance"],
            })
        else:
            snip = summarize(r.get("resolution_text") or r.get("body_summary"), limit=180)
            if snip:
                hints.append(f"Case {r.get('case_number') or '?'} "
                             f"({r.get('resolution_kind')}): {snip}")

    return {"citable": citable, "hints": hints[:5], "scanned": len(rows)}


def count_for_tenant(sb, tenant_id: str) -> int:
    try:
        res = (sb.table("case_memory").select("case_sf_id", count="exact")
               .eq("tenant_id", str(tenant_id)).eq("status", "active")
               .limit(1).execute())
        return res.count or 0
    except Exception:  # noqa: BLE001
        return 0
