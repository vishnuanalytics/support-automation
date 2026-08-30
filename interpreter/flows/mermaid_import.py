"""
Phase 19a -- parse a Mermaid ``flowchart`` / ``graph`` diagram into a
candidate flow graph. Pure + offline: no LLM, no network. Lets someone
sketch a support flow in Mermaid (or paste one an LLM/teammate wrote)
and drop it on the editor canvas instead of hand-placing every node.

Understood subset:

  * node shapes ``A[x]`` ``A(x)`` ``A{x}`` ``A([x])`` ``A[[x]]`` ``A[(x)]``
    ``A{{x}}`` ``A>x]`` and bare ``A``;
  * links ``-->`` ``---`` ``==>`` ``-.->`` with an optional ``|label|``
    or the inline ``A -- label --> B`` / ``A == label ==> B`` /
    ``A -. label .-> B`` forms;
  * chains ``A --> B --> C`` and fan-out/-in ``A --> B & C`` / ``A & B --> C``;
  * ``subgraph ... end`` (flattened -- the grouping is dropped, its nodes and
    edges are kept), ``%%`` comments, an optional ``--- title: ... ---``
    front-matter block.

Ignored: ``classDef`` / ``class`` / ``style`` / ``linkStyle`` / ``click`` /
``direction`` lines.

Edge labels are **not** turned into routing conditions -- a Mermaid label is
free text, the interpreter's ``condition.if`` is a checked expression -- so
every labelled edge is reported in ``warnings`` for the author to wire up in
the Inspector. A node whose id/label doesn't map to a known node type is
kept and coerced to ``draft`` (flagged) by :mod:`flow_candidate`.
"""

from __future__ import annotations

import re

from interpreter.flows.flow_candidate import assemble_candidate
from interpreter.registry import known_types

# normalised label/id token  ->  registered node type
_SYNONYMS: dict[str, str] = {
    "retrieve": "retrieve", "search": "retrieve", "searchdocs": "retrieve",
    "rag": "retrieve", "kb": "retrieve", "knowledgebase": "retrieve",
    "knowledge": "retrieve", "retrieval": "retrieve", "docs": "retrieve",
    "searchknowledgebase": "retrieve", "lookupdocs": "retrieve",
    "classify": "classify", "triage": "classify", "categorize": "classify",
    "categorise": "classify", "classification": "classify", "tag": "classify",
    "draft": "draft", "draftreply": "draft", "write": "draft", "answer": "draft",
    "writereply": "draft", "compose": "draft", "generate": "draft",
    "generatereply": "draft", "drafresponse": "draft", "reply": "draft",
    "draftanswer": "draft", "generateanswer": "draft",
    "confidencegate": "confidence_gate", "gate": "confidence_gate",
    "confidence": "confidence_gate", "checkconfidence": "confidence_gate",
    "confidencecheck": "confidence_gate", "scoregate": "confidence_gate",
    "confident": "confidence_gate", "isconfident": "confidence_gate",
    "autoreply": "auto_reply", "autosend": "auto_reply", "send": "auto_reply",
    "sendreply": "auto_reply", "autorespond": "auto_reply", "autoanswer": "auto_reply",
    "sendautomatically": "auto_reply",
    "askhuman": "ask_human", "escalate": "ask_human", "human": "ask_human",
    "humanreview": "ask_human", "review": "ask_human", "asupport": "ask_human",
    "escalatetohuman": "ask_human", "agentreview": "ask_human",
    "handover": "handover", "handoff": "handover", "transfer": "handover",
    "fullhandover": "handover", "handovertohuman": "handover",
    "identify": "identify", "identifysender": "identify", "whois": "identify",
    "resolvesender": "identify", "lookupsender": "identify", "identifycustomer": "identify",
    "clarify": "clarify", "askcustomer": "clarify", "needinfo": "clarify",
    "followup": "clarify", "moreinfo": "clarify", "clarifyquestions": "clarify",
    "askforinfo": "clarify", "requestinfo": "clarify",
    "sfwriteback": "sf_writeback", "salesforce": "sf_writeback", "sf": "sf_writeback",
    "writeback": "sf_writeback", "updatecase": "sf_writeback", "crm": "sf_writeback",
    "updatesalesforce": "sf_writeback", "writetocrm": "sf_writeback",
    "kblookup": "kb_lookup", "internalkb": "kb_lookup", "runbook": "kb_lookup",
    "sop": "kb_lookup", "internalknowledge": "kb_lookup", "checkrunbook": "kb_lookup",
    "extract": "extract", "extractentities": "extract", "entities": "extract",
    "extractfields": "extract", "extractdata": "extract",
    "policygate": "policy_gate", "policy": "policy_gate", "rules": "policy_gate",
    "ruleengine": "policy_gate", "checkpolicy": "policy_gate", "checkrules": "policy_gate",
    "applyrules": "policy_gate",
    "taskdispatch": "task_dispatch", "dispatch": "task_dispatch", "task": "task_dispatch",
    "dispatchtask": "task_dispatch", "createissue": "task_dispatch",
    "githubissue": "task_dispatch", "raiseticket": "task_dispatch", "openissue": "task_dispatch",
}

_DIRECTIVE = re.compile(r"^\s*(?:flowchart|graph)\s+[A-Za-z]{2}\b\s*", re.I)
_IGNORE = re.compile(r"^\s*(?:classDef|class|style|linkStyle|click|direction)\b", re.I)
_SUBGRAPH = re.compile(r"^\s*subgraph\b", re.I)
_END = re.compile(r"^\s*end\s*$", re.I)

# node-shape definitions: id followed by a bracketed label
_NODE_DEF = re.compile(
    r"""(?P<id>[A-Za-z0-9_][\w.\-]*)\s*
        (?:
            \(\[(?P<stadium>[^\]]*)\]\)      |
            \[\[(?P<subroutine>[^\]]*)\]\]   |
            \[\((?P<cyl>[^)]*)\)\]           |
            \{\{(?P<hexagon>[^}]*)\}\}       |
            \[(?P<rect>[^\]]*)\]             |
            \((?P<round>[^)]*)\)             |
            \{(?P<rhombus>[^}]*)\}           |
            >(?P<flag>[^\]]*)\]
        )""",
    re.X,
)

# a canonical link after inline-label normalisation: an arrow with an
# optional trailing |label|.
_ARROW = re.compile(
    r"\s*(?P<arrow>-\.->|-{2,}>|={2,}>|-{3,}|={3,}|-{2,}|={2,}|<-{2,}>)\s*"
    r"(?:\|(?P<label>[^|]*)\|)?\s*"
)

_IDENT = re.compile(r"^[A-Za-z0-9_][\w.\-]*$")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _strip_label(raw: str) -> str:
    s = (raw or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("#quot;", '"').replace("&quot;", '"')
    return re.sub(r"\s+", " ", s).strip()


def _resolve_type(node_id: str, label: str) -> str:
    """Best-effort id/label -> registered node type; '' if nothing matches."""
    known_norm = {_norm(t): t for t in known_types()}
    for cand in (label, node_id):
        key = _norm(cand)
        if not key:
            continue
        if key in known_norm:
            return known_norm[key]
        if key in _SYNONYMS:
            return _SYNONYMS[key]
    for word in re.split(r"[^a-z0-9]+", (label or "").lower()):
        if word in _SYNONYMS:
            return _SYNONYMS[word]
        if word in known_norm:
            return known_norm[word]
    return ""


def _front_matter_title(text: str) -> str | None:
    m = re.match(r"\s*---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return None
    t = re.search(r"^\s*title:\s*(.+?)\s*$", m.group(1), re.M)
    return _strip_label(t.group(1)) if t else None


def _normalise_inline_labels(s: str) -> str:
    s = re.sub(r"-{2,}\s+([^->|][^|]*?)\s+-{2,}>",
               lambda m: f" -->|{m.group(1).strip()}| ", s)
    s = re.sub(r"={2,}\s+([^=>|][^|]*?)\s+={2,}>",
               lambda m: f" ==>|{m.group(1).strip()}| ", s)
    s = re.sub(r"-\.\s+([^.|][^|]*?)\s+\.->",
               lambda m: f" -.->|{m.group(1).strip()}| ", s)
    return s


def mermaid_to_flow(text: str, *, defaults: dict[str, dict] | None = None) -> dict:
    """Parse a Mermaid flowchart into
    ``{"name", "nodes", "edges", "warnings", "errors"}``.

    ``nodes`` / ``edges`` are in the same shape ``GET /api/flows/{id}``
    returns, ready for the editor to load as unsaved canvas state.
    """
    text = text or ""
    name = _front_matter_title(text)
    body = re.sub(r"^\s*---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.S)

    labels: dict[str, str] = {}
    order: list[str] = []
    raw_edges: list[dict] = []
    warnings: list[str] = []
    errors: list[str] = []
    labelled_edges = 0
    unparsed: list[str] = []

    def see(node_id: str) -> None:
        if node_id not in labels:
            labels[node_id] = ""
            order.append(node_id)

    # split on newlines and bare semicolons; drop %% comments
    raw = body.replace("\r\n", "\n")
    raw = re.sub(r"%%.*", "", raw)
    statements: list[str] = []
    for line in raw.split("\n"):
        for part in line.split(";"):
            if part.strip():
                statements.append(part)

    for stmt in statements:
        if _SUBGRAPH.match(stmt) or _END.match(stmt) or _IGNORE.match(stmt):
            continue
        stmt = _DIRECTIVE.sub("", stmt)
        if not stmt.strip():
            continue

        # pull out bracketed node definitions, leaving bare ids behind
        def _take(m: "re.Match[str]") -> str:
            nid = m.group("id")
            text_grp = next((m.group(g) for g in
                             ("stadium", "subroutine", "cyl", "hexagon",
                              "rect", "round", "rhombus", "flag")
                             if m.group(g) is not None), None)
            see(nid)
            if text_grp is not None:
                lbl = _strip_label(text_grp)
                if lbl:
                    labels[nid] = lbl
            return nid

        stmt = _NODE_DEF.sub(_take, stmt)
        stmt = _normalise_inline_labels(stmt)

        arrows = list(_ARROW.finditer(stmt))
        if not arrows:
            leftover = stmt.strip()
            if leftover and not _IDENT.match(leftover) and "&" not in leftover:
                unparsed.append(leftover)
            continue

        # segments between the arrows -> node id groups (split on '&')
        seg_bounds = [0] + [a.end() for a in arrows]
        seg_ends = [a.start() for a in arrows] + [len(stmt)]
        segments = [stmt[seg_bounds[i]:seg_ends[i]] for i in range(len(arrows) + 1)]

        def ids(seg: str) -> list[str]:
            out = []
            for tok in seg.split("&"):
                tok = tok.strip()
                if _IDENT.match(tok):
                    see(tok)
                    out.append(tok)
                elif tok:
                    unparsed.append(tok)
            return out

        groups = [ids(s) for s in segments]
        for i, a in enumerate(arrows):
            lbl = _strip_label(a.group("label") or "")
            if lbl:
                labelled_edges += 1
            for src in groups[i]:
                for dst in groups[i + 1]:
                    raw_edges.append({"source": src, "target": dst})

    if not order:
        return {"name": name, "nodes": [], "edges": [],
                "warnings": [], "errors": ["no nodes found in the diagram"]}

    raw_nodes = [
        {"key": nid, "type": _resolve_type(nid, labels.get(nid, "")),
         "label": labels.get(nid) or nid}
        for nid in order
    ]

    result = assemble_candidate(raw_nodes, raw_edges, defaults=defaults)
    if labelled_edges:
        result["warnings"].insert(0, (
            f"{labelled_edges} edge label(s) were not turned into routing "
            f"conditions (Mermaid labels are free text) -- open each branching "
            f"edge in the Inspector and set its 'if' expression"
        ))
    if unparsed:
        warnings.append("could not parse: " + "; ".join(sorted(set(unparsed))[:8]))
    result["warnings"].extend(warnings)
    result["errors"] = errors + result["errors"]
    result["name"] = name
    return result
