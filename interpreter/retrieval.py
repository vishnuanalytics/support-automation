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
from scraper import get_embedder, get_supabase  # noqa: E402  reuse client + model

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
def dense_search(sb, query_embedding: list[float], k: int) -> list[dict[str, Any]]:
    rows = sb.rpc(
        "match_doc_chunks",
        {"query_embedding": query_embedding, "match_count": k},
    ).execute().data or []
    for r in rows:
        r["_dense_sim"] = r.get("similarity")
    return rows


def sparse_search(sb, query_text: str, k: int) -> list[dict[str, Any]]:
    rows = sb.rpc(
        "match_doc_chunks_fts",
        {"query_text": query_text, "match_count": k},
    ).execute().data or []
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
        from neo4j_sync import get_neo4j_driver

        driver = get_neo4j_driver()
        db = os.environ.get("NEO4J_DATABASE", "neo4j")
        try:
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
        finally:
            driver.close()
    except Exception as e:  # noqa: BLE001 -- graph expansion is optional
        print(f"  [retrieval] graph-expansion skipped: {e}", file=sys.stderr)
        return []

    neighbour_urls = [r["url"] for r in recs]
    if not neighbour_urls:
        return []
    rows = (
        sb.table("doc_chunks")
        .select("chunk_id, doc_url, chunk_index, chunk_text, heading_path, chunk_type, section")
        .in_("doc_url", neighbour_urls)
        .lt("chunk_index", chunks_per_neighbour)
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
    sb=None,
) -> tuple[list[dict[str, Any]], float]:
    """Run the pipeline. Returns (top_k results, top_score in 0..1)."""
    sb = sb or get_supabase()
    qvec = embed_query(query)

    runs = [dense_search(sb, qvec, dense_k)]
    if use_sparse:
        runs.append(sparse_search(sb, query, sparse_k))
    fused = rrf_fuse(runs)

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
        }
        for r in results
    ]
    return slim, round(top_score, 4)
