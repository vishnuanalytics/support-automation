"""
"Ask the graph" — a natural-language question over the Neo4j case graph,
compiled through a **composable query spec**, never free-form Cypher.

Why a spec and not text-to-Cypher: this platform runs one shared Neo4j
Community-edition database (no per-tenant DB, no custom read-only role —
see docs/MULTI_SYSTEM_ARCHITECTURE.md). The only thing keeping tenant A
from reading tenant B's case text is a `WHERE n.tenant_id = $tenant_id`
predicate on every node. So the LLM's job is bounded to
English -> a small JSON spec (`{entity, metric, group_by, filters,
having, order_by, limit}`); `compile_spec()` turns that into Cypher
**deterministically**, bolting `tenant_id` onto every pattern. The LLM
never sees or writes Cypher.

Blast radius is deliberately small: the only reachable nodes are `Case`
(tenant-scoped), the content-free `Module` / `CaseType` / `Agent` nodes
(reached only via a tenant-scoped Case), and `Account` (also
tenant-scoped). `Reply` / `Message` text is **not** exposed here.

Public entrypoint: `answer(question, tenant_id) -> dict`. Every failure
path raises `GraphQueryError` with a user-safe message; nothing here
raises a raw driver error to the caller.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from interpreter import llm

log = logging.getLogger("interpreter.graph_query")

_TIMEOUT_S = float(os.environ.get("GRAPH_QUERY_TIMEOUT_S", "8"))
_LIMIT_MAX = 200
_LIMIT_DEFAULT = 50
_MAX_FILTERS = 6
_MAX_GROUP_BY = 2


class GraphQueryError(Exception):
    """Anything we won't or can't run — carries a user-facing message.

    `unsupported=True` marks the specific case "this question isn't
    expressible as a graph query" (vs. the graph being down / an LLM
    hiccup) — the router in `ask()` uses it to decide whether to fall
    through to RAG.
    """

    def __init__(self, message: str, *, unsupported: bool = False):
        super().__init__(message)
        self.unsupported = unsupported


# ── the allow-lists: the entire surface the LLM can address ────────────
# Each join key -> the Cypher clause that introduces its node. The
# OPTIONAL variant is derived, used when a dimension is grouped-by but not
# filtered (so a Case with no module still lands in an "(unassigned)" bucket).
_JOINS: dict[str, str] = {
    "module": "MATCH (c)-[:ABOUT]->(m:Module)",
    # SubModule / RootCause / Issue are tenant-scoped — the predicate rides
    # in the pattern, like account. Integration is global-shared (a
    # content-free vocab string), like Module.
    "submodule": "MATCH (c)-[:IN_SUBMODULE]->(sm:SubModule {tenant_id: $tenant_id})",
    "root_cause": "MATCH (c)-[:CAUSED_BY]->(rc:RootCause {tenant_id: $tenant_id})",
    "integration": "MATCH (c)-[:INVOLVES]->(ig:Integration)",
    "issue": "MATCH (c)-[:INSTANCE_OF]->(iss:Issue {tenant_id: $tenant_id})",
    "case_type": "MATCH (c)-[:OF_TYPE]->(ct:CaseType)",
    "agent": "MATCH (c)-[:HANDLED_BY]->(ag:Agent)",
    "account": "MATCH (c)-[:FOR_ACCOUNT]->(a:Account {tenant_id: $tenant_id})",
    # the parent / contract account a portal (child account) sits under
    "parent_account": ("MATCH (c)-[:FOR_ACCOUNT]->(:Account)"
                       "-[:CHILD_OF]->(pa:Account {tenant_id: $tenant_id})"),
}
_JOINS_OPTIONAL: dict[str, str] = {
    k: v.replace("MATCH", "OPTIONAL MATCH", 1) for k, v in _JOINS.items()
}
# The variable each join binds — carried through the `WITH` that precedes
# the `WHERE`, so the tenant predicate is a standalone filter and can never
# be absorbed into an OPTIONAL MATCH pattern.
_JOIN_ALIAS: dict[str, str] = {
    "module": "m", "submodule": "sm", "root_cause": "rc", "integration": "ig",
    "issue": "iss", "case_type": "ct", "agent": "ag", "account": "a",
    "parent_account": "pa",
}

# Group-by dimensions. `join` = which relationship it needs (if any);
# `expr` = the Cypher value; `coalesce` = bucket label when the value is null.
_DIMS: dict[str, dict[str, Any]] = {
    "module": {"join": "module", "expr": "m.name", "coalesce": "(unassigned)",
               "help": "the product module/area the case is about"},
    "submodule": {"join": "submodule", "expr": "sm.name", "coalesce": "(none)",
                  "help": "the sub-module / sub-area within a module"},
    "root_cause": {"join": "root_cause", "expr": "rc.label", "coalesce": "(unknown)",
                   "help": "the extracted underlying fault (e.g. 'Salesforce sync failure')"},
    "integration": {"join": "integration", "expr": "ig.name", "coalesce": "(none)",
                    "help": "an external system the case implicated (Salesforce, Zomato, Stripe…)"},
    "issue": {"join": "issue", "expr": "coalesce(iss.title, iss.issue_key)",
              "coalesce": "(none)", "help": "the curated recurring issue / bug this case is an instance of"},
    "case_type": {"join": "case_type", "expr": "ct.name", "coalesce": "(none)",
                  "help": "the case Type picklist value"},
    "agent": {"join": "agent", "expr": "ag.sf_user_id", "coalesce": "(unassigned)",
              "help": "the Salesforce user who handled the case"},
    "account": {"join": "account", "expr": "a.sf_id", "coalesce": "(none)",
                "help": "the customer account (or portal) the case is filed on"},
    "account_name": {"join": "account", "expr": "a.name", "coalesce": "(none)",
                     "help": "the name of that account / portal"},
    "parent_account": {"join": "parent_account", "expr": "pa.sf_id", "coalesce": "(none)",
                       "help": "the parent / contract account a portal sits under"},
    "tier": {"expr": "c.tier", "coalesce": "(none)", "help": "support tier"},
    "status": {"expr": "c.status", "coalesce": "(none)", "help": "case status"},
    "routed_team": {"expr": "c.routed_team", "coalesce": "(none)",
                    "help": "the team the case was routed to"},
    "origin": {"expr": "c.origin", "coalesce": "(none)",
               "help": "channel the case came in on (email / chat / ...)"},
    "resolution_kind": {"expr": "c.resolution_kind", "coalesce": "(none)",
                        "help": "how the case was resolved"},
    "is_closed": {"expr": "c.is_closed", "coalesce": "false", "bool": True,
                  "help": "whether the case is closed"},
    "opened_month": {"expr": "substring(c.opened_at, 0, 7)", "coalesce": "(unknown)",
                     "help": "calendar month the case was opened (YYYY-MM)"},
}

_OPS_SYM = {"eq": "=", "ne": "<>", "gte": ">=", "lte": "<=", "gt": ">", "lt": "<"}

# Filter fields. `type` drives value validation; `ops` is the allowed set.
_FILTERS: dict[str, dict[str, Any]] = {
    "status": {"expr": "c.status", "type": "str", "ops": ["eq", "ne", "in"]},
    "tier": {"expr": "c.tier", "type": "str", "ops": ["eq", "in"]},
    "routed_team": {"expr": "c.routed_team", "type": "str", "ops": ["eq", "in"]},
    "origin": {"expr": "c.origin", "type": "str", "ops": ["eq", "in"]},
    "resolution_kind": {"expr": "c.resolution_kind", "type": "str", "ops": ["eq", "in"]},
    "is_closed": {"expr": "c.is_closed", "type": "bool", "ops": ["eq"]},
    "module": {"join": "module", "expr": "m.name", "type": "str", "ops": ["eq", "in"]},
    "submodule": {"join": "submodule", "expr": "sm.name", "type": "str", "ops": ["eq", "in"]},
    "root_cause": {"join": "root_cause", "expr": "rc.label", "type": "str",
                   "ops": ["eq", "in", "contains"]},
    "integration": {"join": "integration", "expr": "ig.name", "type": "str",
                    "ops": ["eq", "in"]},
    "issue": {"join": "issue", "expr": "iss.issue_key", "type": "str", "ops": ["eq", "in"]},
    "case_type": {"join": "case_type", "expr": "ct.name", "type": "str", "ops": ["eq", "in"]},
    "agent": {"join": "agent", "expr": "ag.sf_user_id", "type": "str", "ops": ["eq"]},
    "account": {"join": "account", "expr": "a.sf_id", "type": "str", "ops": ["eq", "in"]},
    "account_name": {"join": "account", "expr": "a.name", "type": "str",
                     "ops": ["eq", "in", "contains"]},
    "parent_account": {"join": "parent_account", "expr": "pa.sf_id", "type": "str",
                       "ops": ["eq", "in"]},
    "opened_at": {"expr": "c.opened_at", "type": "date", "ops": ["gte", "lte", "gt", "lt"]},
    "closed_at": {"expr": "c.closed_at", "type": "date", "ops": ["gte", "lte", "gt", "lt"]},
    "subject": {"expr": "c.subject", "type": "str", "ops": ["contains"]},
    "has_duplicate": {"type": "bool", "ops": ["eq"],
                      "help": "whether the case is marked a duplicate of an earlier one"},
}

# Metrics over the matched Case set.
_METRICS: dict[str, dict[str, Any]] = {
    "count": {"agg": "count(DISTINCT c)", "col": "count",
              "help": "how many cases match"},
    "list": {"list": True, "help": "list the matching cases (newest first)"},
    "avg_resolution_hours": {
        "col": "avg_resolution_hours",
        "agg": ("round(avg(datetime(c.closed_at).epochSeconds "
                "- datetime(c.opened_at).epochSeconds) / 3600.0, 1)"),
        "prefilter": ("c.is_closed = true AND c.closed_at IS NOT NULL "
                      "AND c.opened_at IS NOT NULL"),
        "help": "mean hours from opened to closed, over closed cases"},
    "distinct_accounts": {"agg": "count(DISTINCT a)", "col": "distinct_accounts",
                          "force_join": "account",
                          "help": "how many distinct customer accounts match"},
    "distinct_submodules": {"agg": "count(DISTINCT sm.name)", "col": "distinct_submodules",
                            "force_join": "submodule",
                            "help": ("how many distinct sub-modules the matched cases span "
                                     "— group_by module/case_type + having > 1 to find "
                                     "issues that cut across the sub-module tree")},
    "distinct_integrations": {"agg": "count(DISTINCT ig.name)", "col": "distinct_integrations",
                              "force_join": "integration",
                              "help": "how many distinct external systems the matched cases implicate"},
    "duplicate_count": {"agg": "count(DISTINCT dup)", "col": "duplicate_count",
                        "help": "how many earlier cases the matches are duplicates of"},
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ].*)?$")
# Any hint of a write / procedure call / schema op -> refuse outright. This
# is belt-and-braces; compile_spec never emits these, but a compiled string
# is checked before it touches the driver.
_FORBIDDEN_RE = re.compile(
    r"(?i)\b(CREATE|MERGE|DELETE|REMOVE|DETACH|DROP|CALL|FOREACH|GRANT|REVOKE|"
    r"LOAD\s+CSV|USING\s+PERIODIC|SET)\b")


# ── LLM: English -> spec ──────────────────────────────────────────────
def _system_prompt() -> str:
    dims = "\n".join(f"  - {k}: {v.get('help', k)}" for k, v in _DIMS.items())
    filt = "\n".join(
        f"  - {k} (ops: {', '.join(v['ops'])})"
        + (f" — {v['help']}" if v.get("help") else "")
        for k, v in _FILTERS.items())
    mets = "\n".join(f"  - {k}: {v['help']}" for k, v in _METRICS.items())
    return (
        "You translate a support manager's question into a JSON query spec "
        "over a graph of support cases. Output ONLY the JSON object, nothing else.\n\n"
        "Shape:\n"
        '{"entity":"case","metric":<metric>,"group_by":[<dim>...],'
        '"filters":[{"field":<field>,"op":<op>,"value":<value>}...],'
        '"having":{"op":<eq|gte|lte|gt|lt>,"value":<number>},'
        '"order_by":{"field":<"value"|dim>,"dir":<"asc"|"desc">},"limit":<int>}\n\n'
        f"metric (pick one):\n{mets}\n\n"
        f"group_by dimensions (0-2, omit for a single number):\n{dims}\n\n"
        f"filter fields:\n{filt}\n\n"
        "Rules: entity is always \"case\". Use only the names above — never "
        "invent a field, dimension, or value. Dates are ISO strings "
        "(YYYY-MM-DD). 'in' takes a list. 'having' only with a group_by. "
        "'list' cannot be grouped. Omit keys you don't need. If the question "
        "can't be expressed with these names, output {\"error\":\"unsupported\"}.\n\n"
        "Examples:\n"
        "Q: how many billing cases were escalated last month\n"
        'A: {"metric":"count","filters":[{"field":"module","op":"eq","value":"Billing"},'
        '{"field":"status","op":"eq","value":"escalated"},'
        '{"field":"opened_at","op":"gte","value":"2026-08-01"}]}\n'
        "Q: which accounts have more than 2 duplicate cases\n"
        'A: {"metric":"count","group_by":["account"],'
        '"filters":[{"field":"has_duplicate","op":"eq","value":true}],'
        '"having":{"op":"gte","value":2},"order_by":{"field":"value","dir":"desc"}}\n'
        "Q: average resolution time by tier for the API module\n"
        'A: {"metric":"avg_resolution_hours","group_by":["tier"],'
        '"filters":[{"field":"module","op":"eq","value":"API"}]}\n'
        "Q: billing cases where the root cause was a Salesforce sync failure\n"
        'A: {"metric":"list","filters":[{"field":"module","op":"eq","value":"Billing"},'
        '{"field":"root_cause","op":"contains","value":"salesforce sync"}]}\n'
        "Q: which modules have cases spanning more than one sub-module\n"
        'A: {"metric":"distinct_submodules","group_by":["module"],'
        '"having":{"op":"gt","value":1}}\n'
        "Q: case count per portal under account 001ABC\n"
        'A: {"metric":"count","group_by":["account_name"],'
        '"filters":[{"field":"parent_account","op":"eq","value":"001ABC"}]}\n'
        "Q: how many cases on Acme  (a company / customer name, not a module)\n"
        'A: {"metric":"count","filters":['
        '{"field":"account_name","op":"contains","value":"Acme"}]}\n'
        "Q: cases for United Oil & Gas\n"
        'A: {"metric":"list","filters":['
        '{"field":"account_name","op":"contains","value":"United Oil"}]}\n'
        "Q: case count by account\n"
        'A: {"metric":"count","group_by":["account_name"]}\n')


def to_spec(question: str, tenant_id: str | None = None) -> dict:
    if not llm.available(tenant_id=tenant_id):
        raise GraphQueryError(
            "Natural-language questions need an LLM configured; none is available.")
    raw = llm.complete(_system_prompt(), question.strip()[:600],
                       json_object=True, max_tokens=400, temperature=0.0,
                       tenant_id=tenant_id)
    try:
        spec = json.loads(raw)
    except (ValueError, TypeError):
        raise GraphQueryError("Couldn't turn that into a graph query — try rephrasing.",
                              unsupported=True)
    if not isinstance(spec, dict):
        raise GraphQueryError("Couldn't turn that into a graph query — try rephrasing.",
                              unsupported=True)
    if spec.get("error"):
        raise GraphQueryError(
            "That question can't be answered from the case graph yet — "
            "try asking about case counts, accounts, modules, tiers, agents or timing.",
            unsupported=True)
    return spec


# ── validation: spec -> normalised spec (or raise) ────────────────────
def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return bool(v)


def _check_value(field: str, ftype: str, op: str, value: Any) -> Any:
    if op == "in":
        if not isinstance(value, (list, tuple)) or not value:
            raise GraphQueryError(f"filter '{field}' with 'in' needs a non-empty list")
        return [str(x) for x in value]
    if ftype == "bool":
        return _as_bool(value)
    if ftype == "date":
        if not isinstance(value, str) or not _DATE_RE.match(value):
            raise GraphQueryError(f"filter '{field}' needs an ISO date (YYYY-MM-DD)")
        return value
    if isinstance(value, (list, dict)):
        raise GraphQueryError(f"filter '{field}' needs a single value")
    return str(value) if ftype == "str" else value


def validate_spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise GraphQueryError("query spec must be an object")

    if spec.get("entity", "case") != "case":
        raise GraphQueryError("only 'case' questions are supported")

    metric = spec.get("metric") or "count"
    if metric not in _METRICS:
        raise GraphQueryError(f"unknown metric '{metric}'")

    group_by = spec.get("group_by") or []
    if not isinstance(group_by, list):
        raise GraphQueryError("group_by must be a list")
    if len(group_by) > _MAX_GROUP_BY:
        raise GraphQueryError(f"at most {_MAX_GROUP_BY} group_by dimensions")
    for d in group_by:
        if d not in _DIMS:
            raise GraphQueryError(f"unknown group_by dimension '{d}'")
    if metric == "list":
        group_by = []

    raw_filters = spec.get("filters") or []
    if not isinstance(raw_filters, list):
        raise GraphQueryError("filters must be a list")
    if len(raw_filters) > _MAX_FILTERS:
        raise GraphQueryError(f"at most {_MAX_FILTERS} filters")
    filters: list[dict] = []
    for f in raw_filters:
        if not isinstance(f, dict):
            raise GraphQueryError("each filter must be an object")
        field = f.get("field")
        fd = _FILTERS.get(field)
        if not fd:
            raise GraphQueryError(f"unknown filter field '{field}'")
        op = f.get("op")
        if op not in fd["ops"]:
            raise GraphQueryError(
                f"filter '{field}' does not support op '{op}' "
                f"(allowed: {', '.join(fd['ops'])})")
        value = _check_value(field, fd["type"], op, f.get("value"))
        filters.append({"field": field, "op": op, "value": value})

    having = None
    raw_having = spec.get("having")
    if raw_having is not None:
        if metric == "list" or not group_by:
            raise GraphQueryError("'having' only applies with a group_by aggregation")
        if not isinstance(raw_having, dict) or raw_having.get("op") not in _OPS_SYM:
            raise GraphQueryError("'having' needs {op, value}")
        try:
            hv = float(raw_having.get("value"))
        except (TypeError, ValueError):
            raise GraphQueryError("'having' value must be a number")
        having = {"op": raw_having["op"], "value": hv}

    order_by = None
    raw_ob = spec.get("order_by")
    if raw_ob is not None:
        if not isinstance(raw_ob, dict):
            raise GraphQueryError("order_by must be an object")
        of = raw_ob.get("field") or "value"
        if of not in ("value", _METRICS[metric].get("col"), *group_by):
            raise GraphQueryError(f"cannot order by '{of}'")
        od = (raw_ob.get("dir") or "desc").lower()
        if od not in ("asc", "desc"):
            raise GraphQueryError("order_by dir must be asc or desc")
        order_by = {"field": of, "dir": od}

    try:
        limit = int(spec.get("limit") or _LIMIT_DEFAULT)
    except (TypeError, ValueError):
        limit = _LIMIT_DEFAULT
    limit = max(1, min(_LIMIT_MAX, limit))

    return {"entity": "case", "metric": metric, "group_by": group_by,
            "filters": filters, "having": having, "order_by": order_by,
            "limit": limit}


# ── compile: normalised spec -> (cypher, params) ─────────────────────
def compile_spec(spec: dict, tenant_id: str) -> tuple[str, dict]:
    params: dict[str, Any] = {"tenant_id": str(tenant_id)}
    metric = spec["metric"]
    m = _METRICS[metric]

    where = ["c.tenant_id = $tenant_id"]
    required: list[str] = []
    if m.get("force_join"):
        required.append(m["force_join"])
    if m.get("prefilter"):
        where.append(m["prefilter"])

    for i, f in enumerate(spec["filters"]):
        fd = _FILTERS[f["field"]]
        if fd.get("join"):
            required.append(fd["join"])
        if f["field"] == "has_duplicate":
            cond = "EXISTS { MATCH (c)-[:DUPLICATE_OF]->(:Case {tenant_id: $tenant_id}) }"
            where.append(cond if f["value"] else f"NOT {cond}")
            continue
        pname = f"f{i}"
        expr, op = fd["expr"], f["op"]
        if op == "in":
            where.append(f"{expr} IN ${pname}")
        elif op == "contains":
            where.append(f"toLower({expr}) CONTAINS toLower(${pname})")
        else:
            where.append(f"{expr} {_OPS_SYM[op]} ${pname}")
        params[pname] = f["value"]

    optional: list[str] = []
    for d in spec["group_by"]:
        j = _DIMS[d].get("join")
        if j and j not in required and j not in optional:
            optional.append(j)
    required = list(dict.fromkeys(required))
    optional = [j for j in optional if j not in required]

    lines = ["MATCH (c:Case)"]
    lines += [_JOINS[j] for j in required]
    lines += [_JOINS_OPTIONAL[j] for j in optional]
    carried = ["c"] + [_JOIN_ALIAS[j] for j in (*required, *optional)]
    if metric == "duplicate_count":
        lines.append("OPTIONAL MATCH (c)-[:DUPLICATE_OF]->(dup:Case {tenant_id: $tenant_id})")
        carried.append("dup")
    # A `WITH` before the `WHERE` forces a clause boundary: without it, a
    # `WHERE` sitting after an OPTIONAL MATCH binds to that pattern instead
    # of filtering the row, which would silently drop the tenant scoping.
    lines.append("WITH " + ", ".join(carried))
    lines.append("WHERE " + " AND ".join(where))

    params["limit"] = spec["limit"]

    if metric == "list":
        lines.append(
            "RETURN c.case_number AS case_number, c.subject AS subject, "
            "c.status AS status, c.tier AS tier, c.routed_team AS routed_team, "
            "c.opened_at AS opened_at, c.closed_at AS closed_at")
        lines.append("ORDER BY coalesce(c.opened_at, '') DESC")
        lines.append("LIMIT $limit")
        return "\n".join(lines), params

    agg = m["agg"]
    group_by = spec["group_by"]
    if group_by:
        gcols = []
        for k, d in enumerate(group_by):
            dd = _DIMS[d]
            e = dd["expr"]
            if dd.get("join") and dd["join"] in optional:
                e = f"coalesce({e}, '{dd['coalesce']}')"
            elif not dd.get("join"):
                e = f"coalesce(toString({e}), '{dd['coalesce']}')"
            gcols.append((f"g{k}", e))
        lines.append("WITH " + ", ".join(f"{e} AS {a}" for a, e in gcols)
                     + f", {agg} AS value")
        if spec["having"]:
            h = spec["having"]
            lines.append(f"WHERE value {_OPS_SYM[h['op']]} $having")
            params["having"] = h["value"]
        lines.append("RETURN " + ", ".join(a for a, _ in gcols) + ", value")
        ob = spec["order_by"] or {"field": "value", "dir": "desc"}
        of = ("value" if ob["field"] in ("value", m.get("col"))
              else f"g{group_by.index(ob['field'])}")
        lines.append(f"ORDER BY {of} {ob['dir'].upper()}")
        lines.append("LIMIT $limit")
    else:
        lines.append(f"RETURN {agg} AS {m.get('col', 'value')}")

    return "\n".join(lines), params


def _assert_safe(cypher: str) -> None:
    if _FORBIDDEN_RE.search(cypher):
        raise GraphQueryError("refusing to run a non-read-only query")
    if "c.tenant_id = $tenant_id" not in cypher:
        raise GraphQueryError("compiled query is not tenant-scoped")


# ── run ──────────────────────────────────────────────────────────────
def _driver_or_none():
    if not os.environ.get("NEO4J_URI"):
        return None
    try:
        from ingestion.neo4j_sync import get_neo4j_driver
        return get_neo4j_driver()
    except Exception as e:  # noqa: BLE001
        log.warning("graph_query: neo4j driver unavailable: %s", e)
        return None


def _run(cypher: str, params: dict) -> tuple[list[str], list[dict]]:
    from neo4j import Query, RoutingControl

    driver = _driver_or_none()
    if driver is None:
        raise GraphQueryError("The knowledge graph isn't connected for this workspace.")
    from interpreter.case_memory import graph_database
    db = graph_database(params.get("tenant_id"))
    try:
        result = driver.execute_query(
            Query(cypher, timeout=_TIMEOUT_S), params,
            routing_=RoutingControl.READ, database_=db)
    except Exception as e:  # noqa: BLE001
        log.warning("graph_query run failed: %s", e)
        raise GraphQueryError("The graph query failed to run.") from e
    records, _summary, keys = result
    return list(keys), [r.data() for r in records]


def answer(question: str, tenant_id: str) -> dict:
    """English question -> tenant-scoped read-only graph query -> rows.

    Returns {question, spec, cypher, columns, rows, truncated}. Raises
    GraphQueryError (user-safe message) on any refusal or failure."""
    q = (question or "").strip()
    if not q:
        raise GraphQueryError("Ask a question about your support cases.")
    if not os.environ.get("NEO4J_URI"):
        raise GraphQueryError("The knowledge graph isn't connected for this workspace.")

    spec = validate_spec(to_spec(q, tenant_id))
    cypher, params = compile_spec(spec, tenant_id)
    _assert_safe(cypher)
    columns, rows = _run(cypher, params)
    return {
        "question": q,
        "spec": spec,
        "cypher": cypher,
        "columns": columns,
        "rows": rows,
        "truncated": len(rows) >= spec["limit"],
    }


# ── router: graph, else RAG ──────────────────────────────────────────
_METRIC_PHRASE = re.compile(
    r"(?i)\b(how many|how much|count|number of|total|sum|average|avg|mean|"
    r"per |by (module|tier|team|agent|account|month|type|status|integration)|"
    r"most|least|top \d|fewest|trend|over time|breakdown|distribution)\b")


def _looks_like_metric(q: str) -> bool:
    return bool(_METRIC_PHRASE.search(q or ""))


def _rag_fallback(question: str, tenant_id: str, sb) -> dict:
    """Similar resolved cases from `case_memory` — the RAG answer for a
    question the graph can't express."""
    from interpreter import case_memory
    if sb is None:
        from ingestion.scraper import get_supabase
        sb = get_supabase()
    hit = case_memory.lookup(sb, (question or "").strip(),
                             tenant_id=str(tenant_id), k=5, pool=15)
    return {
        "mode": "rag",
        "question": (question or "").strip(),
        "citable": hit.get("citable", []),
        "hints": hit.get("hints", []),
        "scanned": hit.get("scanned", 0),
    }


_RELATED_CYPHER = """
MATCH (seed:Case {case_number: $cn, tenant_id: $tenant_id})
OPTIONAL MATCH (seed)-[:INSTANCE_OF]->(iss:Issue)<-[:INSTANCE_OF]-(oi:Case {tenant_id: $tenant_id})
OPTIONAL MATCH (seed)-[:DUPLICATE_OF]-(od:Case {tenant_id: $tenant_id})
WITH seed,
     [x IN collect(DISTINCT oi) WHERE x.case_number <> seed.case_number] AS by_issue,
     [x IN collect(DISTINCT od) WHERE x.case_number <> seed.case_number] AS by_dup,
     head(collect(iss.title)) AS issue_title
UNWIND (by_issue + by_dup) AS rel
RETURN DISTINCT rel.case_number AS case_number, rel.subject AS subject,
       rel.status AS status, rel.opened_at AS opened_at,
       (rel IN by_issue) AS same_issue, (rel IN by_dup) AS duplicate,
       issue_title AS issue_title
ORDER BY coalesce(rel.opened_at, '') DESC
LIMIT $limit
"""


def related_cases(case_number: str, tenant_id: str, *, limit: int = 50) -> dict:
    """Cases linked to `case_number` by a shared `(:Issue)` (same root cause)
    or a `DUPLICATE_OF` edge — "what's the same underlying bug as #1423".
    Both ends are tenant-scoped. Read-only.

    Returns `{seed, issue_title, related: [{case_number, subject, status,
    opened_at, same_issue, duplicate}]}`.
    """
    cn = (case_number or "").strip()
    if not cn:
        raise GraphQueryError("Give a case number.")
    if not os.environ.get("NEO4J_URI"):
        raise GraphQueryError("The knowledge graph isn't connected for this workspace.")
    params = {"cn": cn, "tenant_id": str(tenant_id), "limit": max(1, min(int(limit), _LIMIT_MAX))}
    _cols, rows = _run(_RELATED_CYPHER, params)
    title = rows[0].get("issue_title") if rows else None
    return {
        "seed": cn,
        "issue_title": title,
        "related": [{k: r.get(k) for k in
                     ("case_number", "subject", "status", "opened_at",
                      "same_issue", "duplicate")} for r in rows],
    }


def ask(question: str, tenant_id: str, *, sb=None) -> dict:
    """Router (feature-req #7). Try the case graph; fall through to RAG
    (`case_memory.lookup`) when the question isn't expressible as a graph
    query, when the graph isn't connected, or when the graph returns
    nothing and the question doesn't read like a metric.

    Return carries `mode: "graph" | "rag"`. A graph miss that fell through
    keeps its (empty) attempt under `graph` for transparency. Real
    safety / execution failures still raise `GraphQueryError`.
    """
    q = (question or "").strip()
    if not q:
        raise GraphQueryError("Ask a question about your support cases.")

    spec = cypher = params = None
    if os.environ.get("NEO4J_URI"):
        try:
            spec = validate_spec(to_spec(q, tenant_id))
            cypher, params = compile_spec(spec, tenant_id)
            _assert_safe(cypher)
        except GraphQueryError as e:
            # not expressible (bad/`error` spec) -> RAG; a safety or compile
            # failure on an otherwise-valid spec is a real bug -> surface.
            if not (getattr(e, "unsupported", False) or spec is None):
                raise
            spec = None

    if spec is None:
        return _rag_fallback(q, tenant_id, sb)

    columns, rows = _run(cypher, params)
    res = {
        "mode": "graph", "question": q, "spec": spec, "cypher": cypher,
        "columns": columns, "rows": rows, "truncated": len(rows) >= spec["limit"],
    }
    if rows or _looks_like_metric(q):
        return res
    rag = _rag_fallback(q, tenant_id, sb)
    rag["graph"] = res
    return rag
