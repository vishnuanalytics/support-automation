# Retrieval eval

Cheap, high-signal check that ingestion (chunking + embeddings) actually
produces retrievable content, and a fixed baseline to measure Phase 2
retrieval tuning against.

## Files

- **`qrels.jsonl`** — 48 questions, one JSON object per line:
  `{"id", "question", "relevant_urls": [...], "section"}`. `relevant_urls`
  are the doc URLs that should be retrieved for that question (usually one;
  a few have two acceptable docs). Hand-written from the live corpus.
- **`run_eval.py`** — embeds each question with the scraper's model
  (`BAAI/bge-small-en-v1.5` + bge query instruction), ranks all
  `doc_chunks` by cosine similarity, collapses to unique doc URLs, and
  reports `hit@k` (k = 1/3/5/10) and `MRR@10`.

## Run

```
python eval/run_eval.py      # needs .env (SUPABASE_URL, SUPABASE_SERVICE_KEY)
```

## Baseline — dense only (2026-08-28, 3568 chunks)

| metric | value |
|---|---|
| hit@1 | 0.896 (43/48) |
| hit@3 | 1.000 |
| hit@5 | 1.000 |
| MRR@10 | 0.944 |

Dense retrieval alone already puts the right doc in the top 3 for every
question on this small, clean corpus. The 5 questions that miss at rank 1
are near-duplicate doc pairs (e.g. a `build/` guide vs. its `reference/`
tutorial).

## Not covered yet (Phase 2)

Sparse (Postgres FTS on `doc_chunks.fts`), dense+sparse RRF fusion, Neo4j
graph-expansion, and cross-encoder rerank. When the Phase 2
`match_doc_chunks` / hybrid SQL functions exist, add them as extra
strategies in `run_eval.py` and compare against this baseline.
