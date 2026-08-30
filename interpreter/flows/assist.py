"""
Phase 19b / 19c -- LLM-assisted flow authoring.

  * ``assist_generate(prompt)``            -- a plain-English description ->
                                              a whole candidate flow graph.
  * ``assist_edit(current, instruction)``  -- an existing graph + an
                                              instruction -> a rewritten
                                              candidate graph + a diff.

Both go through :func:`flow_candidate.assemble_candidate`, so an unknown
node type is coerced to ``draft`` (flagged), the graph is structurally
validated, and **nothing is persisted** -- the API hands the candidate
straight to the editor canvas, and Save / Publish go through the normal
path.

Provider default is Groq (CLAUDE.md). With no key, ``llm.complete`` returns
the deterministic stub, whose ``assist`` branch makes this work offline and
in CI.
"""

from __future__ import annotations

import json
import re
from typing import Any

from interpreter import llm
from interpreter.flows.flow_candidate import assemble_candidate
from interpreter.flows.flow_diff import diff_graphs
from interpreter.registry import known_types

# one-liners so the model picks sensible types. A registered type that's not
# listed here still works -- this only steers the model.
_TYPE_DOC: dict[str, str] = {
    "retrieve": "search the knowledge base for context (usually the start)",
    "identify": "resolve the sender: CRM contact / email-domain -> account / unknown",
    "classify": "triage the case: tier, topic, urgency, summary",
    "extract": "pull named fields out of the message into state.entities",
    "kb_lookup": "consult an internal runbook collection at a checkpoint",
    "policy_gate": "evaluate the team's when->then rules; can override routing",
    "draft": "write the reply from retrieved context; sets draft_confidence + groundedness",
    "confidence_gate": "score the draft against a per-tier threshold; a pass/fail branch point",
    "sf_writeback": "write triage fields back onto the Salesforce case",
    "auto_reply": "send the drafted reply automatically (terminal)",
    "ask_human": "hand to a human with the draft attached (terminal)",
    "handover": "full handover, nothing sent (terminal)",
    "clarify": "ask the customer the specific missing questions (terminal)",
    "task_dispatch": "raise a Slack-approved internal task, e.g. a GitHub issue (terminal)",
}

_CONDITION_NAMES = (
    "tier, region, confidence, retrieval_score, draft_confidence, "
    "confidence_gate.pass, classification.topic, classification.urgency, "
    "policy.action, sender.known, entities.<name>"
)

_SHAPE = (
    '{\n'
    '  "name": "<short name>",\n'
    '  "nodes": [{"key": "<unique short slug>", "type": "<a type below>", '
    '"label": "<human label>", "config": {<optional>}}],\n'
    '  "edges": [{"source": "<key>", "target": "<key>", '
    '"if": "<boolean expression or null>"}]\n'
    '}'
)

_RULES = (
    "- exactly one node has no incoming edge (the start) -- usually `retrieve`.\n"
    "- terminal nodes (auto_reply / ask_human / handover / clarify / task_dispatch) "
    "have no outgoing edge.\n"
    "- a branch node has several outgoing edges: at most one with `\"if\": null` "
    "(the else), the rest with an expression.\n"
    "- keep it minimal: only the nodes the request needs. No cycles.\n"
    "- omit `config` unless a specific value is asked for -- defaults are filled in.\n"
    f"- names usable in an `if`: {_CONDITION_NAMES}. "
    "e.g. \"tier == 'enterprise'\", \"confidence_gate.pass\", \"not confidence_gate.pass\"."
)


def _type_list() -> str:
    return "\n".join(f"  {t}: {_TYPE_DOC.get(t, '')}".rstrip() for t in sorted(known_types()))


_SYSTEM_GENERATE = (
    "You design flows for a support-automation platform. A flow is a directed "
    "acyclic graph of typed nodes. Output ONLY a JSON object of this shape:\n\n"
    f"{_SHAPE}\n\nRules:\n{_RULES}\n\nNode types:\n" + _type_list()
)

_SYSTEM_EDIT = (
    "You edit an existing support-automation flow. You are given the current "
    "graph as JSON and a change to make. Output ONLY the COMPLETE updated flow "
    "as a JSON object of this shape (plus an optional \"summary\" string):\n\n"
    f"{_SHAPE}\n\nRules:\n{_RULES}\n"
    "- keep the `key` (it is the node id) of every node you keep; invent a new "
    "short slug only for genuinely new nodes.\n\nNode types:\n" + _type_list()
)


def _parse(raw: str) -> dict:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", raw or "", re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def _candidate_from(data: dict, defaults: dict | None,
                    fallback_nodes: list | None = None,
                    fallback_edges: list | None = None) -> dict:
    nodes = data.get("nodes")
    edges = data.get("edges")
    return assemble_candidate(
        nodes if isinstance(nodes, list) and nodes else (fallback_nodes or []),
        edges if isinstance(edges, list) else (fallback_edges or []),
        defaults=defaults,
    )


def assist_generate(prompt: str, *, defaults: dict | None = None,
                    model: str | None = None) -> dict:
    """Plain-English description -> candidate flow graph."""
    raw = llm.complete(
        system=_SYSTEM_GENERATE,
        user=(prompt or "").strip() or "(no description given)",
        model=model or llm.DEFAULT_MODEL,
        json_object=True,
        max_tokens=1400,
    )
    data = _parse(raw)
    res = _candidate_from(data, defaults)

    if res["errors"]:
        repair_user = (
            "Your previous flow had these structural problems:\n- "
            + "\n- ".join(res["errors"])
            + "\n\nHere is what you produced:\n"
            + json.dumps({k: data.get(k) for k in ("name", "nodes", "edges")}, indent=2)
            + "\n\nReturn a corrected COMPLETE flow in the same JSON shape."
        )
        fixed_raw = llm.complete(
            system=_SYSTEM_GENERATE, user=repair_user,
            model=model or llm.DEFAULT_MODEL, json_object=True, max_tokens=1400,
        )
        fixed = _candidate_from(_parse(fixed_raw), defaults)
        if len(fixed["errors"]) < len(res["errors"]):
            data, res = _parse(fixed_raw), fixed

    res["name"] = (str(data.get("name") or "").strip() or None)
    res["summary"] = None
    res["diff"] = None
    return res


def assist_edit(current: dict, instruction: str, *, defaults: dict | None = None,
                model: str | None = None) -> dict:
    """Existing flow graph + an instruction -> a rewritten candidate + a diff."""
    cur_nodes = [
        {"key": n["node_id"], "type": n["type"],
         "label": n.get("label") or n["type"], "config": n.get("config") or {}}
        for n in current.get("nodes", [])
    ]
    cur_edges = [
        {"source": e["source_node_id"], "target": e["target_node_id"],
         "if": (e.get("condition") or {}).get("if")}
        for e in current.get("edges", [])
    ]
    user = (
        "# Current flow\n"
        + json.dumps({"nodes": cur_nodes, "edges": cur_edges}, indent=2)
        + "\n\n# Change to make\n"
        + ((instruction or "").strip() or "(no instruction given)")
        + "\n\nReturn the COMPLETE updated flow in the same JSON shape. Keep the "
        "`key` of every node you keep; use a new short slug for new nodes."
    )
    raw = llm.complete(
        system=_SYSTEM_EDIT, user=user, model=model or llm.DEFAULT_MODEL,
        json_object=True, max_tokens=1800,
    )
    data = _parse(raw)
    res = _candidate_from(data, defaults, fallback_nodes=cur_nodes, fallback_edges=cur_edges)
    res["summary"] = (str(data.get("summary") or "").strip() or None)
    res["diff"] = diff_graphs(
        current, {"nodes": res["nodes"], "edges": res["edges"]}
    )
    res["name"] = None
    return res
