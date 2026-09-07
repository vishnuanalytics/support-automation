"""
Hybrid retrieval for the `retrieve` node.

Pipeline (each stage optional via config):

    dense  (pgvector / HNSW, match_doc_chunks)          \
                                                          >- RRF fuse
    sparse (Postgres FTS,    match_doc_chunks_fts)       /
                          |
                          v
    graph-expansion  (Neo4j LINKS_TO neighbours of the top docs, pulled
                      back in as extra candidates with a small prior)
                          |
                          v
    cross-encoder rerank  (fastembed TextCrossEncoder, local ONNX, free)
                          |
                          v
    top_k chunks  +  top_score  (reranked score of the #1 chunk, squashed
                                 to 0..1 -- this is what feeds confidence_gate)

Everything is local/free: bge-small query embedding via fastembed (shared
with the scraper), FTS in Postgres, ms-marco-MiniLM cross-encoder via
fastembed. No paid API.
"""

from __future__ import annotations

import math
import os
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from ingestion.scraper import get_embedder, get_supabase  # noqa: E402  reuse client + model

# bge-small-en-v1.5: prepend to queries only (matches eval/run_eval.py).
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"

_reranker = None


def get_reranker():
    global _reranker
    if _reranker is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        _reranker = TextCrossEncoder(model_name=RERANK_MODEL)
    return _reranker


def embed_query(text: str) -> list[float]:
    vec = next(iter(get_embedder().embed([QUERY_INSTRUCTION + text])))
    return [float(x) for x in vec]


# --------------------------------------------------------------------------
# Individual stages
# --------------------------------------------------------------------------
# A source_id that can't exist — returned instead of `None` for a
# tenant-scoped call that resolves to zero sources, so retrieval matches
# nothing (`None` would search *every* tenant's chunks).
_NO_MATCH = ["00000000-0000-0000-0000-000000000000"]


def resolve_sources(names: list[str] | None, sb, tenant_id: str | None = None) -> list[str] | None:
    """
    Map a `retrieve` node's `kb_sources` to source_ids, scoped so a flow can
    only ever reach **its own tenant's** sources — plus any **shared/global**
    source it *explicitly names*.

      names given        -> those names, intersected with (shared | this tenant);
                            if none of them are visible, fall back to the
                            tenant's own sources (never widen, never leak).
      names None + tenant -> this tenant's own **org-level** sources only
                            (a collection with `config.org_level == false` is
                            node-scoped: it's read only when a node names it).
                            Shared/global corpora (e.g. the `zapier-public`
                            demo docs) are opt-in — a flow must list them.
      names None, no tenant -> shared sources only (eval/admin; those callers
                            pass source_ids directly if they want everything).

    A tenant-scoped call that resolves to nothing returns `_NO_MATCH`, never
    `None` — an empty scope must mean "no KB context", not "search all tenants".
    """
    rows = (sb.table("sources").select("source_id, name, tenant_id, config")
            .eq("status", "active").execute().data or [])
    if names:
        visible = [r for r in rows if r["tenant_id"] is None or r["tenant_id"] == tenant_id]
        named = [r["source_id"] for r in visible if r["name"] in names]
        if named:
            return named
        # named nothing visible -> fall through to the tenant's org-level scope

    def _org_level(r: dict) -> bool:
        cfg = r.get("config") or {}
        # the org KB is always org-level; every other collection is unless it
        # was explicitly toggled off. Missing flag == on (back-compat).
        return bool(cfg.get("org_kb")) or cfg.get("org_level", True) is not False

    if tenant_id:
        own = [r["source_id"] for r in rows
               if r["tenant_id"] == tenant_id and _org_level(r)]
        return own or _NO_MATCH

    shared = [r["source_id"] for r in rows if r["tenant_id"] is None]
    return shared or None


def dense_search(sb, query_embedding: list[float], k: int,
                 source_ids: list[str] | None = None) -> list[dict[str, Any]]:
    args = {"query_embedding": query_embedding, "match_count": k}
    if source_ids:
        args["p_source_ids"] = source_ids
    rows = sb.rpc("match_doc_chunks", args).execute().data or []
    for r in rows:
        r["_dense_sim"] = r.get("similarity")
    return rows


def sparse_search(sb, query_text: str, k: int,
                  source_ids: list[str] | None = None) -> list[dict[str, Any]]:
    args = {"query_text": query_text, "match_count": k}
    if source_ids:
        args["p_source_ids"] = source_ids
    rows = sb.rpc("match_doc_chunks_fts", args).execute().data or []
    for r in rows:
        r["_fts_rank"] = r.get("rank")
    return rows


def rrf_fuse(
    runs: list[list[dict[str, Any]]],
    *,
    rrf_k: int = 60,
    weights: list[float] | None = None,
) -> list[dict[str, Any]]:
    """Reciprocal-rank fusion, keyed on chunk_id. Returns candidates sorted
    best-first with a `_rrf` score attached."""
    weights = weights or [1.0] * len(runs)
    scores: dict[str, float] = {}
    keep: dict[str, dict[str, Any]] = {}
    for run, w in zip(runs, weights):
        for rank, row in enumerate(run, start=1):
            cid = row["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + w * (1.0 / (rrf_k + rank))
            keep.setdefault(cid, row)
    fused = []
    for cid, s in sorted(scores.items(), key=lambda kv: -kv[1]):
        row = dict(keep[cid])
        row["_rrf"] = s
        fused.append(row)
    return fused


def graph_expand(
    sb,
    seed_doc_urls: list[str],
    *,
    max_neighbours: int = 5,
    chunks_per_neighbour: int = 2,
) -> list[dict[str, Any]]:
    """Pull chunks from docs that the seed docs link to (Neo4j LINKS_TO).
    Best-effort: if Neo4j isn't configured/reachable, return []."""
    if not seed_doc_urls or not os.environ.get("NEO4J_URI"):
        return []
    try:
        from ingestion.neo4j_sync import get_neo4j_driver

        driver = get_neo4j_driver()   # cached singleton — do NOT close here
        db = os.environ.get("NEO4J_DATABASE", "neo4j")
        recs = driver.execute_query(
            """
            MATCH (d:Doc)-[:LINKS_TO]->(n:Doc)
            WHERE d.url IN $urls AND NOT n.url IN $urls
              AND coalesce(n.status, 'active') <> 'deleted'
            RETURN n.url AS url, count(*) AS w
            ORDER BY w DESC
            LIMIT $lim
            """,
            urls=seed_doc_urls,
            lim=max_neighbours,
            database_=db,
        ).records
    except Exception as e:  # noqa: BLE001 -- graph expansion is optional
        print(f"  [retrieval] graph-expansion skipped: {e}", file=sys.stderr)
        return []

    neighbour_urls = [r["url"] for r in recs]
    if not neighbour_urls:
        return []
    rows = (
        sb.table("doc_chunks")
        .select("chunk_id, doc_url, chunk_index, chunk_text, heading_path, "
                "chunk_type, section, entry_status")
        .in_("doc_url", neighbour_urls)
        .lt("chunk_index", chunks_per_neighbour)
        .neq("entry_status", "superseded")           # P1b — never expand into stale chunks
        .execute()
        .data
        or []
    )
    for r in rows:
        r["_graph"] = True
    return rows


def rerank(query: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not candidates:
        return []
    scores = list(get_reranker().rerank(query, [c["chunk_text"] for c in candidates]))
    out = []
    for c, s in zip(candidates, scores):
        row = dict(c)
        row["rerank_score"] = float(s)
        out.append(row)
    out.sort(key=lambda r: -r["rerank_score"])
    return out


def _squash(x: float) -> float:
    """Cross-encoder logit -> 0..1. ms-marco-MiniLM outputs roughly [-11, 11]."""
    return 1.0 / (1.0 + math.exp(-x))


_QUALITY_WEIGHT = {"official": 1.15, "community_resolved": 1.0, "unverified": 0.9}


def _apply_quality_weights(sb, rows: list[dict[str, Any]]) -> None:
    """Nudge fused RRF scores by the source-trust signal on each chunk's KB
    entry (`kb_entries.quality`, set from `KBDocument.quality`): an official
    help article outranks a random forum reply that scored similarly. In
    place, then re-sorts. Best-effort — a lookup failure leaves scores as
    they were. Only KB chunks (`kb://<sid>/<eid>` doc_url) are affected;
    the shared `zapier-public` corpus and any real-URL doc keep weight 1.0."""
    try:
        eids = {u.rsplit("/", 1)[-1] for r in rows
                if (u := r.get("doc_url") or "").startswith("kb://")}
        if not eids:
            return
        q = {row["entry_id"]: (row.get("quality") or "unverified")
             for row in (sb.table("kb_entries").select("entry_id, quality")
                         .in_("entry_id", list(eids)).execute().data or [])}
        for r in rows:
            u = r.get("doc_url") or ""
            if u.startswith("kb://"):
                w = _QUALITY_WEIGHT.get(q.get(u.rsplit("/", 1)[-1], "unverified"), 1.0)
                r["_rrf"] = (r.get("_rrf") or 0.0) * w
        rows.sort(key=lambda r: -(r.get("_rrf") or 0.0))
    except Exception as e:  # noqa: BLE001
        print(f"  [retrieval] quality weighting skipped: {e}", file=sys.stderr)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def hybrid_retrieve(
    query: str,
    *,
    top_k: int = 5,
    dense_k: int = 30,
    sparse_k: int = 30,
    use_sparse: bool = True,
    use_graph: bool = True,
    use_rerank: bool = True,
    kb_sources: list[str] | None = None,   # source *names* (Phase 12)
    tenant_id: str | None = None,          # scopes which sources are reachable
    sb=None,
) -> tuple[list[dict[str, Any]], float]:
    """Run the pipeline. Returns (top_k results, top_score in 0..1)."""
    sb = sb or get_supabase()
    qvec = embed_query(query)
    source_ids = resolve_sources(kb_sources, sb, tenant_id) if (kb_sources or tenant_id) else None

    runs = [dense_search(sb, qvec, dense_k, source_ids)]
    if use_sparse:
        runs.append(sparse_search(sb, query, sparse_k, source_ids))
    fused = rrf_fuse(runs)
    _apply_quality_weights(sb, fused)

    if use_graph:
        seed_urls = list(dict.fromkeys(r["doc_url"] for r in fused[:5]))
        extra = graph_expand(sb, seed_urls)
        seen = {r["chunk_id"] for r in fused}
        fused.extend(r for r in extra if r["chunk_id"] not in seen)

    pool = fused[: max(top_k * 6, 30)]

    if use_rerank and pool:
        ranked = rerank(query, pool)
        top_score = _squash(ranked[0]["rerank_score"]) if ranked else 0.0
        results = ranked[:top_k]
    else:
        results = pool[:top_k]
        # no reranker -> use the fused RRF score of the top hit, scaled to ~0..1
        top_score = min(1.0, (pool[0].get("_rrf") or 0.0) * 30) if pool else 0.0

    slim = [
        {
            "doc_url": r["doc_url"],
            "chunk_index": r.get("chunk_index"),
            "heading_path": r.get("heading_path"),
            "chunk_type": r.get("chunk_type"),
            "section": r.get("section"),
            "chunk_text": r["chunk_text"],
            "rerank_score": r.get("rerank_score"),
            "rrf_score": r.get("_rrf"),
            "from_graph": r.get("_graph", False),
            "entry_status": r.get("entry_status") or "active",   # P1b
        }
        for r in results
    ]
    return slim, round(top_score, 4)
