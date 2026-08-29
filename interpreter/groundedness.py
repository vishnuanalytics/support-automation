"""
Is the draft actually supported by the retrieved context?

The `draft` node is *told* to use only the provided context; nothing checks
that it did. A confidently-hallucinated reply otherwise sails through the
gate whenever retrieval scored well. `check()` returns a 0..1 score the
`confidence_gate` can weight in (`groundedness_weight` in its config).

Two backends, same shape:
  - Groq judge (when GROQ_API_KEY is set): claim-level entailment.
  - lexical fallback (no key): fraction of the draft's content words that
    appear in the retrieved chunks. Crude, deterministic, still catches a
    draft that wandered off-corpus.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import llm

_WORD = re.compile(r"[a-z0-9]{4,}")
_STOP = {
    "this", "that", "with", "from", "your", "will", "have", "here", "they",
    "them", "then", "when", "what", "which", "would", "could", "should",
    "there", "their", "about", "into", "also", "been", "were", "these", "those",
    "based", "documentation", "context", "steps", "reply", "please", "thanks",
}


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def _lexical(draft: str, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    d = _content_words(draft)
    if not d:
        return {"score": 1.0, "backend": "lexical", "unsupported": []}
    corpus = _content_words(" ".join(c.get("chunk_text", "") for c in chunks))
    unsupported = sorted(d - corpus)
    score = round(1.0 - len(unsupported) / len(d), 4)
    return {"score": score, "backend": "lexical", "unsupported": unsupported[:15]}


def _judge(draft: str, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    context = "\n\n---\n\n".join(c.get("chunk_text", "") for c in chunks[:6]) or "(none)"
    raw = llm.complete(
        system=(
            "You check whether a support reply is grounded in the given docs. "
            "List the reply's factual claims, then for each say if the docs "
            'support it. Return JSON {"supported": int, "total": int, '
            '"unsupported_claims": [string]}.'
        ),
        user=f"# Reply\n{draft}\n\n# Docs\n{context}",
        model=llm.FAST_MODEL,
        json_object=True,
        max_tokens=400,
    )
    try:
        p = json.loads(raw)
        total = max(int(p.get("total", 0)), 1)
        score = round(int(p.get("supported", 0)) / total, 4)
        return {"score": min(score, 1.0), "backend": "groq",
                "unsupported": p.get("unsupported_claims", [])[:15]}
    except (json.JSONDecodeError, TypeError, ValueError):
        return _lexical(draft, chunks)


def check(draft: str, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    if not draft or not chunks:
        return {"score": 0.0, "backend": "none", "unsupported": []}
    return _judge(draft, chunks) if llm.available() else _lexical(draft, chunks)
