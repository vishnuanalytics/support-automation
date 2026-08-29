"""
Retrieval eval — recall/MRR over eval/qrels.jsonl, by strategy.

Strategies
----------
  dense          bge-small (fastembed) cosine over all chunks, ranked in numpy.
                 The Phase 1 baseline. Default; no DB function needed.
  sparse         Postgres FTS  -> match_doc_chunks_fts   (007)
  hybrid         dense+sparse RRF fused in SQL -> match_doc_chunks_hybrid (007)
  hybrid_rerank  hybrid candidates, then a local ms-marco-MiniLM cross-encoder
                 rerank (fastembed TextCrossEncoder). Matches what the
                 interpreter's `retrieve` node does.

All ranked chunks are collapsed to unique doc URLs; a question "hits" at k if
a gold URL is in the top-k docs.

Metrics (averaged over all questions):
  hit@k   k = 1,3,5,10       (== recall@k here; most questions have 1 gold doc)
  MRR@10

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (loaded from .env by scraper.py).
Run:
  python eval/run_eval.py                     # dense baseline
  python eval/run_eval.py --strategy hybrid
  python eval/run_eval.py --strategy all      # every strategy, side by side
"""

import argparse
import json
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scraper import get_embedder, get_supabase  # noqa: E402  reuse client + model

QRELS_PATH = pathlib.Path(__file__).with_name("qrels.jsonl")
# bge-small-en-v1.5: prepend this to queries only (not passages) for s2p retrieval.
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
KS = (1, 3, 5, 10)
MRR_CUTOFF = 10
PAGE = 1000              # PostgREST max rows per request
CHUNK_POOL = 300         # top chunk hits to collapse into a doc ranking
RPC_CHUNKS = 60          # chunks pulled per query for the DB-backed strategies
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
STRATEGIES = ("dense", "sparse", "hybrid", "hybrid_rerank")


def load_qrels() -> list[dict]:
    rows = []
    with open(QRELS_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if not rows:
        sys.exit(f"no questions found in {QRELS_PATH}")
    return rows


# --------------------------------------------------------------------------
# dense: in-process numpy ranking (unchanged baseline)
# --------------------------------------------------------------------------
def fetch_chunk_matrix(sb) -> tuple[np.ndarray, np.ndarray]:
    """Return (doc_urls[N], embeddings[N, 384] float32, L2-normalised)."""
    urls: list[str] = []
    vecs: list[list[float]] = []
    start = 0
    while True:
        page = (
            sb.table("doc_chunks")
            .select("doc_url, embedding")
            .range(start, start + PAGE - 1)
            .execute()
            .data
        )
        if not page:
            break
        for row in page:
            emb = row["embedding"]
            if isinstance(emb, str):          # pgvector serialises as "[...]" over PostgREST
                emb = json.loads(emb)
            urls.append(row["doc_url"])
            vecs.append(emb)
        if len(page) < PAGE:
            break
        start += PAGE

    mat = np.asarray(vecs, dtype=np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12
    print(f"loaded {len(urls)} chunks from doc_chunks")
    return np.asarray(urls), mat


def embed_queries(questions: list[str]) -> np.ndarray:
    vecs = list(get_embedder().embed([QUERY_INSTRUCTION + q for q in questions]))
    mat = np.asarray([np.asarray(v, dtype=np.float32) for v in vecs])
    mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12
    return mat


def dense_docs(sims: np.ndarray, urls: np.ndarray, limit: int) -> list[str]:
    """Top chunk sims -> unique doc URLs, best-first."""
    pool = min(CHUNK_POOL, sims.shape[0])
    top = np.argpartition(-sims, pool - 1)[:pool]
    top = top[np.argsort(-sims[top])]
    out: list[str] = []
    for u in urls[top]:
        if u not in out:
            out.append(u)
            if len(out) >= limit:
                break
    return out


# --------------------------------------------------------------------------
# DB-backed strategies (007 functions) + optional local rerank
# --------------------------------------------------------------------------
_reranker = None


def get_reranker():
    global _reranker
    if _reranker is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        _reranker = TextCrossEncoder(model_name=RERANK_MODEL)
    return _reranker


def _collapse(rows: list[dict], limit: int) -> list[str]:
    out: list[str] = []
    for r in rows:
        u = r["doc_url"]
        if u not in out:
            out.append(u)
            if len(out) >= limit:
                break
    return out


def sparse_docs(sb, question: str, limit: int) -> list[str]:
    rows = sb.rpc(
        "match_doc_chunks_fts", {"query_text": question, "match_count": RPC_CHUNKS}
    ).execute().data or []
    return _collapse(rows, limit)


def hybrid_rows(sb, question: str, qvec: list[float]) -> list[dict]:
    return sb.rpc(
        "match_doc_chunks_hybrid",
        {
            "query_text": question,
            "query_embedding": qvec,
            "match_count": RPC_CHUNKS,
            "dense_count": RPC_CHUNKS,
            "sparse_count": RPC_CHUNKS,
        },
    ).execute().data or []


def hybrid_docs(sb, question: str, qvec: list[float], limit: int) -> list[str]:
    return _collapse(hybrid_rows(sb, question, qvec), limit)


def hybrid_rerank_docs(sb, question: str, qvec: list[float], limit: int) -> list[str]:
    rows = hybrid_rows(sb, question, qvec)
    if not rows:
        return []
    scores = list(get_reranker().rerank(question, [r["chunk_text"] for r in rows]))
    order = sorted(range(len(rows)), key=lambda i: -scores[i])
    return _collapse([rows[i] for i in order], limit)


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------
def score(ranked_per_q: list[list[str]], qrels: list[dict], label: str) -> None:
    n = len(qrels)
    hits = {k: 0 for k in KS}
    rr_total = 0.0
    misses: list[tuple[str, str, str]] = []

    for q, docs in zip(qrels, ranked_per_q):
        gold = set(q["relevant_urls"])
        rank = next((i + 1 for i, u in enumerate(docs) if u in gold), None)
        rr_total += 1.0 / rank if rank and rank <= MRR_CUTOFF else 0.0
        for k in KS:
            if rank and rank <= k:
                hits[k] += 1
        if rank is None or rank > 5:
            misses.append((q["id"], q["question"], docs[0] if docs else "-"))

    print(f"\n{label} — {n} questions\n" + "-" * 52)
    for k in KS:
        print(f"  hit@{k:<2} {hits[k] / n:6.3f}   ({hits[k]}/{n})")
    print(f"  MRR@{MRR_CUTOFF} {rr_total / n:6.3f}")
    if misses:
        print(f"  {len(misses)} question(s) with no gold doc in top 5:")
        for qid, question, got in misses:
            print(f"    [{qid}] {question[:70]}")
            print(f"          top1 -> {got}")


# --------------------------------------------------------------------------
def run_strategy(name: str, sb, qrels: list[dict], dense_cache: dict) -> None:
    limit = max(KS)
    if name == "dense":
        urls, chunk_mat = dense_cache["urls"], dense_cache["mat"]
        q_mat = dense_cache["q_mat"]
        ranked = [dense_docs(chunk_mat @ qv, urls, limit) for qv in q_mat]
        score(ranked, qrels, "dense (bge-small-en-v1.5, numpy)")
        return

    qvecs = dense_cache["q_list"]
    if name == "sparse":
        ranked = [sparse_docs(sb, q["question"], limit) for q in qrels]
        score(ranked, qrels, "sparse (Postgres FTS)")
    elif name == "hybrid":
        ranked = [hybrid_docs(sb, q["question"], v, limit) for q, v in zip(qrels, qvecs)]
        score(ranked, qrels, "hybrid (dense+sparse RRF, SQL)")
    elif name == "hybrid_rerank":
        ranked = [hybrid_rerank_docs(sb, q["question"], v, limit) for q, v in zip(qrels, qvecs)]
        score(ranked, qrels, "hybrid + cross-encoder rerank (ms-marco-MiniLM)")
    else:
        sys.exit(f"unknown strategy: {name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--strategy", default="dense",
        choices=(*STRATEGIES, "all"),
        help="retrieval strategy to score (default: dense)",
    )
    args = ap.parse_args()

    qrels = load_qrels()
    sb = get_supabase()
    questions = [q["question"] for q in qrels]

    # embed queries once; dense also needs the full chunk matrix
    q_mat = embed_queries(questions)
    dense_cache = {
        "q_mat": q_mat,
        "q_list": [v.tolist() for v in q_mat],
        "urls": None,
        "mat": None,
    }
    wanted = STRATEGIES if args.strategy == "all" else (args.strategy,)
    if "dense" in wanted:
        urls, mat = fetch_chunk_matrix(sb)
        dense_cache["urls"], dense_cache["mat"] = urls, mat

    for name in wanted:
        run_strategy(name, sb, qrels, dense_cache)


if __name__ == "__main__":
    main()
