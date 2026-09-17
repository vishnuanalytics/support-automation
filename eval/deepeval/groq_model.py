"""
A deepeval judge model backed by `interpreter/llm.py` — the same Groq
(`openai/gpt-oss-*`) roster the pipeline itself uses.

deepeval ships with an OpenAI-GPT-4 judge by default. This repo has no
OpenAI key and a "free/local tooling, LLM calls default to Groq"
convention (see CLAUDE.md), so every deepeval metric here is scored by
Groq instead, routed through the project's own `llm.complete` (fallback
chain, rate-limit backoff, in-process cache all included).

deepeval 4.x calls the judge in two shapes:
  * `generate(prompt, schema=<pydantic model>)` — structured; must return
    an instance of that model (deepeval then reads its fields directly).
  * `generate(prompt)` — free text; return a plain string.
Both are handled below; on a schema-parse miss we hand back the raw JSON
string and let deepeval's own `trimAndLoadJson` have a go.
"""

from __future__ import annotations

import json
from typing import Any

from deepeval.models.base_model import DeepEvalBaseLLM
from pydantic import BaseModel

from interpreter import llm

_SYS_JSON = (
    "You are a meticulous evaluation judge. Follow the instructions exactly "
    "and reply with ONLY a single JSON object that matches the requested "
    "shape — no prose, no code fences."
)
_SYS_TEXT = (
    "You are a meticulous evaluation judge. Follow the instructions exactly "
    "and answer concisely."
)


class GroqJudge(DeepEvalBaseLLM):
    """Judge over `interpreter/llm.py`.

    `model` in the llm roster  → routed normally through `llm.complete`
    (Groq default, with its fallback chain + backoff).

    `model` NOT in the roster  → assumed an OpenRouter slug and called
    directly via `llm._openrouter_complete`, bypassing the Groq-then-
    fallback churn. Use this (`--judge-model nvidia/nemotron-3-ultra-550b-a55b:free`)
    when Groq's free-tier TPM is saturated — slow (~100 s/call) but does
    not depend on Groq at all.
    """

    def __init__(self, model: str | None = None, *, max_tokens: int = 1200,
                 temperature: float = 0.0) -> None:
        self.model_id = model or llm.DEFAULT_MODEL
        self._openrouter = self.model_id not in llm.MODELS
        self._max_tokens = max_tokens
        self._temperature = temperature
        super().__init__(model=self.model_id)

    def load_model(self) -> "GroqJudge":
        return self

    def get_model_name(self) -> str:
        via = "openrouter, direct" if self._openrouter else "groq, via interpreter.llm"
        return f"{self.model_id} ({via})"

    def _raw(self, system: str, user: str, *, json_object: bool) -> str:
        if self._openrouter:
            return llm._openrouter_complete(
                system, user, self.model_id, self._max_tokens,
                self._temperature, json_object)
        return llm.complete(
            system=system, user=user, model=self.model_id,
            max_tokens=self._max_tokens, temperature=self._temperature,
            json_object=json_object)

    def generate(self, prompt: str, schema: type[BaseModel] | None = None,
                 **_: Any) -> Any:
        if schema is None:
            return self._raw(_SYS_TEXT, prompt, json_object=False)
        raw = self._raw(
            _SYS_JSON + "\n\n# Required JSON schema\n"
            + json.dumps(schema.model_json_schema()),
            prompt, json_object=True)
        try:
            return schema.model_validate_json(raw)
        except Exception:
            try:
                return schema.model_validate(json.loads(llm._coerce_json(raw) or raw))
            except Exception:
                return raw  # let deepeval's trimAndLoadJson try

    async def a_generate(self, prompt: str, schema: type[BaseModel] | None = None,
                         **kw: Any) -> Any:
        # llm.complete is sync; run it inline. Keeps judge calls serialised,
        # which is what we want against Groq's free-tier TPM ceiling anyway.
        return self.generate(prompt, schema=schema, **kw)
