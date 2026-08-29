# Retrieval eval

Cheap, high-signal check that ingestion (chunking + embeddings) actually
produces retrievable content, and a fixed baseline to measure Phase 2
retrieval tuning against.

## Files

- **`qrels.jsonl`** — 48 questions, one JSON object per line:
  `{"id", "question", "relevant_urls": [...], "section"}`. `relevant_urls`
  are the doc URLs that should be retrieved for that question (usually one;
  a few have two acceptable docs). Hand-written from the live corpus.
- **`run_eval.py`** — for each strategy, ranks `doc_chunks`, collapses to
  unique doc URLs, and reports `hit@k` (k = 1/3/5/10) and `MRR@10`.

## Run

```
python -m ingestion.eval.run_eval                     # dense baseline (default)
python -m ingestion.eval.run_eval --strategy hybrid
python -m ingestion.eval.run_eval --strategy all      # all four, side by side
```
Needs `.env` (`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`). `sparse` / `hybrid` /
`hybrid_rerank` call the `007` SQL functions; `hybrid_rerank` also loads the
local `ms-marco-MiniLM-L-6-v2` cross-encoder (`--strategy all` ≈ 6 min on
CPU, most of it the rerank).

## Strategies

| strategy | how |
|---|---|
| `dense` | `bge-small-en-v1.5` cosine over all chunks, ranked in numpy (Phase 1 baseline) |
| `sparse` | Postgres FTS on `doc_chunks.fts` → `match_doc_chunks_fts` |
| `hybrid` | dense + sparse, RRF-fused in SQL → `match_doc_chunks_hybrid` |
| `hybrid_rerank` | `hybrid` candidates, then local cross-encoder rerank — what `interpreter/retrieval.py` runs |

## Results (2026-08-29, 3568 chunks, 48 questions)

| strategy | hit@1 | hit@3 | hit@5 | MRR@10 |
|---|---|---|---|---|
| dense (baseline) | **0.896** | **1.000** | 1.000 | **0.944** |
| sparse (FTS) | 0.562 | 0.667 | 0.708 | 0.613 |
| hybrid (RRF, SQL) | 0.771 | 0.958 | 0.958 | 0.861 |
| hybrid + rerank | 0.896 | 1.000 | 1.000 | 0.941 |

**Read:** on this small, clean, well-written corpus dense retrieval is
already at ceiling (right doc in top-3 for every question). Sparse alone is
much weaker — the questions are paraphrases, not keyword matches, so
vocabulary mismatch hurts. Naively RRF-fusing sparse *in* pulls MRR down
(0.944 → 0.861): the weak lexical ranks displace correct dense hits. The
cross-encoder rerank on top then re-floats them and lands right back at the
dense baseline (MRR 0.941). So hybrid+rerank costs ~nothing here and is what
the interpreter uses because it degrades gracefully as the corpus grows
noisier and as queries get more keyword-shaped (error strings, API names,
version numbers) — none of which this 400-doc eval set exercises.

## Not covered yet

Neo4j graph-expansion is wired in `interpreter/retrieval.py` but not scored
here (it adds recall, not top-1 precision, so it barely moves these metrics
on a corpus where dense is already at ceiling). A larger / noisier qrels set
that actually separates the strategies would be the next eval investment.
