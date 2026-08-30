# Authoring a flow without drawing it (Phase 19)

The React Flow canvas is still there, but you don't have to place every node
by hand. Three entry points produce a **candidate graph** that lands on the
canvas as an *unsaved draft* — you review it, tweak it, then **Save draft**
/ **Publish** like any other edit. Nothing is persisted until you save, and
Save still runs the full structural validation (`replace_flow_graph`).

| Entry point | Where | What it does |
|---|---|---|
| **⬇ From Mermaid** | flow list · editor toolbar (*Import Mermaid*) | Deterministic parser (`interpreter/flows/mermaid_import.py`), no LLM. Paste a `flowchart` diagram. |
| **✨ From prompt** | flow list | One Groq call (`assist.assist_generate`). Describe the flow in a sentence or two. |
| **✨ AI edit** | editor toolbar | One Groq call (`assist.assist_edit`) over the current draft + your instruction; shows an add/remove/change diff in the banner. |

All three go through `interpreter/flows/flow_candidate.assemble_candidate`,
which: assigns a real uuid per node (an existing uuid is kept, so an **AI
edit** preserves node identity), merges per-type default config under
anything supplied, and runs `check_flow` + the builder's "exactly one start
node" rule — splitting the result into **errors** (block Save) and
**warnings** (advisory).

## How Mermaid maps

- **Node type** comes from the node's id or its label, matched against the
  handler registry plus a synonym table (`"check confidence"` →
  `confidence_gate`, `"escalate to human"` → `ask_human`, `"draft reply"` →
  `draft`, …). A node that matches nothing is **kept and set to `draft`**,
  and flagged in the warnings — open it in the Inspector and pick the right
  type.
- **Edges** carry topology only. A Mermaid edge label (`-->|yes|`) is free
  text; the interpreter's branch conditions are checked expressions
  (`confidence_gate.pass`, `tier == 'enterprise'`). Labelled edges are
  reported in the warnings — open each branching edge in the Inspector and
  set its `if`.
- Supported: node shapes `[] () {} ([]) [[]] [()] {{}} >]`, links
  `--> --- ==> -.->` with `|label|` or the inline `A -- label --> B` form,
  chains `A --> B --> C`, fan `A --> B & C`, `subgraph ... end` (flattened),
  `%%` comments, and an optional `--- title: ... ---` front-matter block
  (used as the suggested flow name).

```
flowchart TD
  R[retrieve] --> C[classify] --> D[draft] --> G{confidence gate}
  G -->|pass| A[auto reply]
  G -->|fail| H[escalate to human]
```

## How the AI paths behave

- **Provider** is Groq by default (per `CLAUDE.md`); with no key the
  deterministic `llm` stub returns a minimal valid flow, so tests and
  offline demos work. The system prompt enumerates the registered node
  types, the names usable in an `if` expression, and the shape rules.
- **generate** does one repair round-trip if its first graph is
  structurally broken (kept only if it comes back with fewer errors).
- **edit** asks for the *complete* rewritten graph and keeps the `key`
  (= node id) of every node it retains; new nodes get fresh ids. The diff
  is computed by id and shown as counts + changed labels.

## Notes / limits

- If you belong to **more than one tenant**, the assist / import endpoints
  need a `tenant_id` (same 400 as **＋ New flow**).
- The edit diff can cosmetically mark a bare node (empty stored config) as
  *changed* when per-type defaults get merged in on the way back. It's
  advisory — you're reviewing the graph on the canvas before saving.
- `validate_flow.EXPECTED_TYPES` is unchanged: import / AI don't redefine
  what makes a flow "complete", they just get you a draft faster.
