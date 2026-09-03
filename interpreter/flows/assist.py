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
    "trigger": "entry node for a webhook/schedule flow (no Case): config.map renames "
               "incoming fields, config.required + config.defaults validate/fill "
               "state.context (a missing required field sets input._missing, doesn't fail)",
    "retrieve": "search the knowledge base for context (usually the start)",
    "identify": "resolve the sender: CRM contact / email-domain -> account / unknown",
    "case_lookup": "pull similar past RESOLVED cases so draft answers from real "
                   "resolutions, not just KB docs -- output is "
                   "case_lookup.prior_resolutions / case_lookup.investigation_hints "
                   "(lists; there is no .found field)",
    "sf_case": "resolve/create the Salesforce Case for an inbound message (thread-based "
               "reuse) -- needed before sf_writeback / ask_human / handover can act on a real Case",
    "sf_context": "load Account/Contact/Case-history/team around the Case into "
                  "state.sf_context (config.want); place after identify",
    "classify": "triage the case: tier, topic, urgency, summary",
    "extract": "pull named fields out of the message into state.entities -- REQUIRES "
               "config.fields = {\"field_name\": \"what it means\"}; with no config.fields "
               "it's a silent no-op (entities stays {}), so always populate it when using "
               "this node, e.g. `\"config\": {\"fields\": {\"product\": \"the product or plan "
               "mentioned\", \"error\": \"the exact error text\"}}`",
    "kb_lookup": "consult an internal runbook collection at a checkpoint",
    "team_route": "pick the owning team from config.rules (keyword match) -> "
                  "state.routed_team; downstream nodes read routed_team -- don't "
                  "re-implement the routing as duplicate if/else edges",
    "policy_gate": "evaluate the team's when->then rules -- these live in the separate "
                   "Rules table (policy_rules), NOT in this node's config, so leave "
                   "config empty. Sets policy.matched / policy.action (a 'route' rule) / "
                   "policy.task (a 'task' rule, feeds a downstream task_dispatch); "
                   "there is no policy.pass",
    "attachments": "fetch + OCR/transcribe image or video attachments (config.source/"
                   "ocr/video) -> state.attachments + attachment_text (folded into "
                   "classify/draft) -- use this for 'read the screenshot' requests",
    "draft": "write the reply from retrieved context; sets draft_confidence + groundedness",
    "ai_prompt": "a fully custom LLM step -- your own system/user prompt template + "
                 "optional json_schema (config.system/user/model) -> ai[output_key]; "
                 "use this instead of draft for a non-standard tone, format, or output",
    "confidence_gate": "score the draft against a per-tier threshold; a pass/fail branch point",
    "sf_writeback": "write triage fields back onto the Salesforce case",
    "http_request": "call an external API through a named per-tenant connection "
                    "(config.connection/method/path/body) -> context[out_key]. "
                    "`{{...}}` templates are dotted paths straight against state, e.g. "
                    "{{context.case_id}} or {{case.subject}} -- never {{state.x}}",
    "transform": "reshape state.context with no LLM: config.map (copy by dotted path, "
                 "e.g. context.http.json.total) / config.set (`{{dotted.path}}` template, "
                 "not {{state.x}}) / config.drop",
    "auto_reply": "send the drafted reply automatically (terminal)",
    "ask_human": "hand to a human with the draft attached (terminal)",
    "handover": "full handover, nothing sent (terminal)",
    "clarify": "ask the customer the specific missing questions (terminal)",
    "notify": "ping an internal rep on Salesforce Chatter ONLY, without changing Case "
              "ownership -- no Slack; use notify_human for Slack",
    "notify_human": "ping a person about an escalation on Slack and/or Chatter -- the "
                    "one to use for 'post to Slack' requests; place after ask_human/"
                    "handover. config.channel picks 'slack'|'salesforce_chatter'|'both' "
                    "(NOT a channel name); the actual Slack channel goes in "
                    "config.slack_channel, e.g. \"#support-escalations\"",
    "task_dispatch": "raise a Slack Approve/Reject card for an internal task (e.g. a "
                     "GitHub issue) from an UPSTREAM policy_gate match (state.policy.task) "
                     "-- the approval happens asynchronously via the Slack callback, NOT as "
                     "a branch/edge in this graph; terminal, no outgoing edge",
}

_CONDITION_NAMES = (
    "tier, region, confidence, retrieval_score, draft_confidence, "
    "confidence_gate.pass, classification.topic, classification.urgency, "
    "routed_team, policy.action, sender.known, entities.<name>"
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
    "- keep it minimal: only the nodes the request needs. No cycles. Don't add a "
    "terminal node the request didn't ask for -- if it doesn't say what happens with "
    "the result, stop after the last node it actually described. `auto_reply` "
    "specifically sends `draft` -- never wire it straight after a node that isn't "
    "`draft` or `ai_prompt`.\n"
    "- when the request gives concrete values for a node's own documented config "
    "(a named mapping, specific rules, a connection/team name, fields to copy) -- "
    "put them in that node's `config`, don't leave it empty. E.g. for team_route: "
    "`\"config\": {\"rules\": [{\"team\": \"billing\", \"any\": [\"invoice\", \"payment\"]}, "
    "...], \"default\": \"support\"}`. Otherwise omit `config` -- defaults are filled in.\n"
    f"- names usable in an `if`: {_CONDITION_NAMES}. "
    "e.g. \"tier == 'enterprise'\", \"confidence_gate.pass\", \"not confidence_gate.pass\". "
    "Only use a dotted field a node actually sets (see its one-liner below) -- never "
    "invent one (e.g. there is no `case_lookup.found`)."
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
    "as a JSON object of this shape, plus a \"summary\" string -- ALWAYS include "
    "it, one sentence saying what you changed and why:\n\n"
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


# `assemble_candidate` never hard-fails an unknown type or a dangling edge --
# it downgrades them to a warning so a broken generation still loads onto the
# canvas for the user to fix by hand. But both are genuine model mistakes
# (a hallucinated type name / a node referenced in an edge but never
# declared), unlike e.g. "several possible start nodes" which can be a
# legitimate multi-root draft -- so only these two are worth a repair round.
_HARD_WARNING_MARKERS = ("isn't in the graph", "is not a known node type")


def _hard_warnings(warnings: list[str]) -> list[str]:
    return [w for w in (warnings or []) if any(m in w for m in _HARD_WARNING_MARKERS)]


def _is_improvement(prior: dict, fixed: dict) -> bool:
    key = lambda r: (len(r["errors"]), len(_hard_warnings(r["warnings"])))  # noqa: E731
    return key(fixed) < key(prior)


def _repair_round(system: str, original_user: str, problems: list[str], prior_data: dict,
                  *, defaults: dict | None, model: str | None, max_tokens: int,
                  fallback_nodes: list | None = None,
                  fallback_edges: list | None = None) -> tuple[dict, dict] | None:
    """One retry: the original request + what's wrong with the model's own
    previous output. Carries the original context (not just the error list)
    so the retry stays faithful to what was actually asked for."""
    if not problems:
        return None
    repair_user = (
        original_user
        + "\n\n# Your previous attempt had problems\n- " + "\n- ".join(problems)
        + "\n\nHere is what you produced:\n"
        + json.dumps({k: prior_data.get(k) for k in ("name", "nodes", "edges", "summary")
                      if k in prior_data}, indent=2)
        + "\n\nReturn a corrected COMPLETE flow in the same JSON shape, still "
        "satisfying the original request above."
    )
    fixed_raw = llm.complete(system=system, user=repair_user, model=model or llm.DEFAULT_MODEL,
                             json_object=True, max_tokens=max_tokens)
    fixed_data = _parse(fixed_raw)
    fixed_res = _candidate_from(fixed_data, defaults,
                                fallback_nodes=fallback_nodes, fallback_edges=fallback_edges)
    return fixed_data, fixed_res


def assist_generate(prompt: str, *, defaults: dict | None = None,
                    model: str | None = None) -> dict:
    """Plain-English description -> candidate flow graph."""
    user = (prompt or "").strip() or "(no description given)"
    raw = llm.complete(
        system=_SYSTEM_GENERATE, user=user,
        model=model or llm.DEFAULT_MODEL,
        json_object=True,
        max_tokens=1400,
    )
    data = _parse(raw)
    res = _candidate_from(data, defaults)

    problems = res["errors"] + _hard_warnings(res["warnings"])
    fixed = _repair_round(_SYSTEM_GENERATE, user, problems, data,
                          defaults=defaults, model=model, max_tokens=1400)
    if fixed and _is_improvement(res, fixed[1]):
        data, res = fixed

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

    problems = res["errors"] + _hard_warnings(res["warnings"])
    fixed = _repair_round(_SYSTEM_EDIT, user, problems, data,
                          defaults=defaults, model=model, max_tokens=1800,
                          fallback_nodes=cur_nodes, fallback_edges=cur_edges)
    if fixed and _is_improvement(res, fixed[1]):
        data, res = fixed

    res["summary"] = (str(data.get("summary") or "").strip() or None)
    res["diff"] = diff_graphs(
        current, {"nodes": res["nodes"], "edges": res["edges"]}
    )
    res["name"] = None
    return res
