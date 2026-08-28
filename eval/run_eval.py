"""
Phase 1 retrieval baseline — dense (pgvector) recall/MRR over eval/qrels.jsonl.

What this scores
----------------
For each question in `qrels.jsonl` we embed the query with the same model the
scraper used (`BAAI/bge-small-en-v1.5` via fastembed, plus the bge query
instruction), rank every chunk in `doc_chunks` by cosine similarity, collapse
the ranked chunks to unique doc URLs, and check whether a gold URL lands in the
top-k docs.

Metrics, averaged over all questions:
  hit@k   — fraction of questions with a gold doc in the top-k  (k = 1,3,5,10)
  MRR@10  — mean reciprocal rank of the first gold doc (0 if outside top 10)

Since almost every question has a single gold doc, hit@k == recall@k here.

Scope
-----
This is the *pre-tuning* baseline and covers the dense half only. The Phase 2
hybrid path (dense + Postgres FTS on `doc_chunks.fts`, fused with RRF, then
Neo4j graph-expansion, then a cross-encoder rerank) is designed but not built —
see PROJECT_SCOPE.md. When the Phase 2 `match_doc_chunks` / hybrid SQL
functions exist, swap `_fetch_chunk_matrix` + `_rank` for RPC calls and add the
sparse/hybrid strategies alongside `dense`.

Embeddings are ranked in-process with numpy (3.5k chunks x 384d ~= 5 MB); no DB
function or migration needed. Fine up to O(100k) chunks.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (loaded from .env by scraper.py).
Run:  python eval/run_eval.py
"""

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


def ranked_docs(sims: np.ndarray, urls: np.ndarray, limit: int) -> list[str]:
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


def main() -> None:
    qrels = load_qrels()
    sb = get_supabase()
    urls, chunk_mat = fetch_chunk_matrix(sb)
    q_mat = embed_queries([q["question"] for q in qrels])

    hits = {k: 0 for k in KS}
    rr_total = 0.0
    misses: list[tuple[str, str, str]] = []

    for q, qv in zip(qrels, q_mat):
        sims = chunk_mat @ qv
        docs = ranked_docs(sims, urls, max(KS))
        gold = set(q["relevant_urls"])
        rank = next((i + 1 for i, u in enumerate(docs) if u in gold), None)

        rr_total += 1.0 / rank if rank and rank <= MRR_CUTOFF else 0.0
        for k in KS:
            if rank and rank <= k:
                hits[k] += 1
        if rank is None or rank > 5:
            misses.append((q["id"], q["question"], docs[0] if docs else "-"))

    n = len(qrels)
    print(f"\nRetrieval baseline — dense (bge-small-en-v1.5), {n} questions\n" + "-" * 52)
    for k in KS:
        print(f"  hit@{k:<2} {hits[k] / n:6.3f}   ({hits[k]}/{n})")
    print(f"  MRR@{MRR_CUTOFF} {rr_total / n:6.3f}")

    if misses:
        print(f"\n{len(misses)} question(s) with no gold doc in top 5:")
        for qid, question, got in misses:
            print(f"  [{qid}] {question}\n        top1 -> {got}")


if __name__ == "__main__":
    main()
