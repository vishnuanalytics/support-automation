# eval/e2e — end-to-end action eval (Phase 7)

The retrieval eval (`ingestion/eval/`) scores whether the right *document*
comes back. This scores the thing the product actually decides: **should
the bot auto-send, ask a human, or hand over.**

## Files

- **`cases.jsonl`** — 22 cases, each `{id, gold_action, why, subject, body,
  account:{customer_type, region}}`. Hand-labelled: well-covered how-tos →
  `auto_reply`; billing / account / legal / commercial → `ask_human`;
  enterprise → `handover` (the tier rule).
- **`run_e2e.py`** — runs each case through the **real** pipeline
  (`build_graph(flow).invoke`), compares `outcome.action` to `gold_action`,
  and prints metrics + a confusion matrix + a **threshold sweep**.

## Run

```
python eval/e2e/run_e2e.py                          # Acme support flow (11111111…)
python eval/e2e/run_e2e.py --tenant <id> --team support
```
Slow (real embed + rerank per case, ~6 s each). `GROQ_API_KEY` set → real
drafts + token counts; unset → stub drafts (`draft_confidence` fixed ~0.65,
so the decision is driven by `retrieval_score`).

## Metrics

| metric | meaning |
|---|---|
| action accuracy | `pred == gold` |
| **auto-send precision** | of cases the bot auto-replied, fraction where gold was also `auto_reply` — "was it safe to send" |
| **escalation precision** | of cases the bot escalated, fraction where gold was not `auto_reply` — "did it actually need a human" |
| coverage | fraction auto-replied |

## Result — stub drafts, 22 cases

**Before calibration** (gate `default_threshold` 0.35, per-tier
{basic .35, premium .45, enterprise .6}):

| | |
|---|---|
| action accuracy | 0.864 |
| auto-send precision | **0.769** (10/13 — 3 unsafe) |
| escalation precision | 1.000 (9/9) |
| coverage | 0.591 |
| latency p50 / p95 | 6.0 / 6.8 s |

The 3 unsafe auto-sends (`e11` SOC2 compliance, `e12` Partner-API access,
`e18` data export) are cases where a doc *is* relevant (retrieval 0.99–1.0)
but the correct answer is "a human / legal / commercial owner handles this."
A confidence threshold can't distinguish *relevant* from *sufficient +
appropriate*.

**Threshold sweep** (uniform `default_threshold`, tier→handover kept):

| t | auto-send P | escalation P | coverage | acc |
|---|---|---|---|---|
| 0.35 | 0.714 | 1.000 | 0.636 | 0.818 |
| 0.45 | 0.769 | 1.000 | 0.591 | 0.864 |
| **0.55** | **0.833** | 1.000 | 0.545 | 0.909 |
| 0.75 | 0.833 | 1.000 | 0.545 | 0.909 |
| 0.80 | 0.750 | 0.714 | 0.364 | 0.727 |

**After calibration** (`011_calibrate_gate.sql` — Acme support gate →
`default_threshold` 0.5, per-tier {basic .5, premium .55, enterprise .6},
`groundedness_weight` 0.2):

| | before → after |
|---|---|
| action accuracy | 0.864 → **0.909** |
| auto-send precision | 0.769 → **0.833** (10/12) |
| escalation precision | 1.000 → 1.000 (10/10) |
| coverage | 0.591 → 0.545 |

Groundedness (Phase 7) penalises a thin draft — this is what flips `e18`
(data export) from a wrong auto-send to `ask_human`. `e11` (SOC2) and `e12`
(Partner API) remain wrong: a doc is genuinely relevant (retrieval 1.0) but
the answer is "a human owns this." Fixing them needs an **intent →
`ask_human` edge** (commercial/legal), a flow-authoring change, not a
threshold — a Phase 7 follow-up.

## Extending

Add cases as real ones surface (e.g. from `runs` where a human heavily
edited the draft — Phase 11). With a Groq key, add an LLM-judge rubric
score on the draft body for the `auto_reply` cases.
