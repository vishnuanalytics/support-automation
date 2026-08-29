"""
FastAPI backend for the Phase 5 React Flow editor.

It is deliberately thin — it reuses interpreter/ for the parts that need
Python (structural validation, compiling + running a flow) and lets Supabase
do auth + tenant isolation:

  every request carries the caller's Supabase access token; flow reads/writes
  go through a Supabase client authed as that user, so the Phase 4 RLS
  policies scope everything. The service-role client is used only for the
  interpreter's own machinery (retrieval, running a compiled graph).

Endpoints (all under /api):
  GET  /health
  GET  /node-types                 palette: known node types + default config
  GET  /flows                      RLS-scoped list
  POST /flows                      create {tenant_id, team, name, status?}
  GET  /flows/{id}                 full flow (nodes + edges), unvalidated
  PUT  /flows/{id}                 save {name,status,version,nodes,edges}; 422 on invalid
  POST /flows/{id}/validate        {valid, errors} for a posted flow dict
  POST /flows/{id}/run             body {case}; compile + invoke, return trace + outcome

Run:  uvicorn api.main:app --reload
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client

load_dotenv()

from interpreter.builder import build_graph  # noqa: E402
from interpreter.flows.validate_flow import Flow, check_flow  # noqa: E402
from interpreter.loader import FlowInvalid, FlowNotFound, load_flow  # noqa: E402
from interpreter.registry import known_types  # noqa: E402

SUPABASE_URL = os.environ["SUPABASE_URL"]
ANON_KEY = os.environ.get("SUPABASE_ANON_KEY") or os.environ["SUPABASE_SERVICE_KEY"]
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
WEB_ORIGINS = os.environ.get("WEB_ORIGINS", "http://localhost:5173").split(",")

app = FastAPI(title="support-automation editor api")
app.add_middleware(
    CORSMiddleware,
    allow_origins=WEB_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

_service = create_client(SUPABASE_URL, SERVICE_KEY)

# default node config for the palette (mirrors the seed flows)
NODE_DEFAULTS: dict[str, dict[str, Any]] = {
    "retrieve": {"source": ["supabase", "neo4j"], "top_k": 5},
    "classify": {"tier_field": "account.customer_type", "region_field": "account.region"},
    "sf_writeback": {
        "object": "Case",
        "field_map": {"urgency": "Priority", "topic": "Module__c", "region": "Region__c"},
        "value_maps": {"Priority": {"critical": "High", "high": "High", "normal": "Medium", "low": "Low"}},
        "append": {"Description": "summary"},
    },
    "draft": {"model": "llama-3.3-70b-versatile", "max_tokens": 500},
    "confidence_gate": {
        "default_threshold": 0.35,
        "tier_overrides": {"basic": 0.35, "premium": 0.45, "enterprise": 0.6},
        "retrieval_weight": 0.5,
    },
    "auto_reply": {},
    "ask_human": {"channel": "salesforce_chatter"},
    "handover": {"reason": "policy"},
}


# ── auth ───────────────────────────────────────────────────────────────
class Caller:
    def __init__(self, token: str):
        self.token = token
        self.user_id = _jwt_sub(token)
        self.sb = create_client(SUPABASE_URL, ANON_KEY)
        self.sb.postgrest.auth(token)          # RLS applies to this client


def _jwt_sub(token: str) -> str | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("sub")
    except Exception:  # noqa: BLE001
        return None


def caller(authorization: str = Header(default="")) -> Caller:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    return Caller(authorization.split(" ", 1)[1].strip())


# ── models ─────────────────────────────────────────────────────────────
class NodeIn(BaseModel):
    node_id: str
    type: str
    label: str | None = None
    position_x: int | None = None
    position_y: int | None = None
    config: dict[str, Any] = {}


class EdgeIn(BaseModel):
    edge_id: str
    source_node_id: str
    target_node_id: str
    condition: dict[str, Any] = {}


class FlowIn(BaseModel):
    name: str
    status: str = "draft"
    version: int = 1
    nodes: list[NodeIn] = []
    edges: list[EdgeIn] = []


class FlowCreate(BaseModel):
    tenant_id: str
    team: str
    name: str
    status: str = "draft"


class RunIn(BaseModel):
    case: dict[str, Any]


# ── helpers ────────────────────────────────────────────────────────────
def _flow_dict(meta: dict, body: FlowIn) -> dict:
    return {
        "flow_id": meta["flow_id"], "tenant_id": meta["tenant_id"],
        "team": meta["team"], "name": body.name, "version": body.version,
        "status": body.status,
        "nodes": [n.model_dump() for n in body.nodes],
        "edges": [e.model_dump() for e in body.edges],
    }


def _structural_errors(flow_dict: dict) -> list[str]:
    try:
        parsed = Flow.model_validate(flow_dict)
    except Exception as e:  # noqa: BLE001
        return [f"shape: {e}"]
    errs = check_flow(parsed, require_expected_types=False)
    unknown = {n["type"] for n in flow_dict["nodes"]} - known_types()
    if unknown:
        errs.append(f"unknown node type(s): {sorted(unknown)}")
    return errs


def _require_visible(c: Caller, flow_id: str) -> dict:
    rows = c.sb.table("flows").select("*").eq("flow_id", flow_id).execute().data or []
    if not rows:
        raise HTTPException(404, "flow not found or not visible to you")
    return rows[0]


# ── endpoints ──────────────────────────────────────────────────────────
@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/node-types")
def node_types() -> dict:
    return {"types": sorted(known_types()), "defaults": NODE_DEFAULTS}


@app.get("/api/flows")
def list_flows(c: Caller = Depends(caller)) -> list[dict]:
    rows = (
        c.sb.table("flows")
        .select("flow_id, tenant_id, team, name, status, version, updated_at")
        .order("tenant_id").order("team").execute().data
        or []
    )
    return rows


@app.post("/api/flows", status_code=201)
def create_flow(body: FlowCreate, c: Caller = Depends(caller)) -> dict:
    fid = str(uuid.uuid4())
    try:
        c.sb.table("flows").insert({
            "flow_id": fid, "tenant_id": body.tenant_id, "team": body.team,
            "name": body.name, "status": body.status, "version": 1,
        }).execute()
    except Exception as e:  # noqa: BLE001  -- RLS / unique-published violation
        raise HTTPException(400, str(e))
    return {"flow_id": fid}


@app.get("/api/flows/{flow_id}")
def get_flow(flow_id: str, c: Caller = Depends(caller)) -> dict:
    _require_visible(c, flow_id)
    try:
        return load_flow(flow_id=flow_id, sb=c.sb, validate=False)
    except FlowNotFound:
        raise HTTPException(404, "flow not found")


@app.post("/api/flows/{flow_id}/validate")
def validate_flow_ep(flow_id: str, body: FlowIn, c: Caller = Depends(caller)) -> dict:
    meta = _require_visible(c, flow_id)
    errs = _structural_errors(_flow_dict(meta, body))
    return {"valid": not errs, "errors": errs}


@app.put("/api/flows/{flow_id}")
def save_flow(flow_id: str, body: FlowIn, c: Caller = Depends(caller)) -> dict:
    meta = _require_visible(c, flow_id)
    fd = _flow_dict(meta, body)
    errs = _structural_errors(fd)
    if errs:
        raise HTTPException(422, {"errors": errs})

    have_nodes = {r["node_id"] for r in
                  c.sb.table("flow_nodes").select("node_id").eq("flow_id", flow_id).execute().data or []}
    have_edges = {r["edge_id"] for r in
                  c.sb.table("flow_edges").select("edge_id").eq("flow_id", flow_id).execute().data or []}
    want_nodes = {n.node_id for n in body.nodes}
    want_edges = {e.edge_id for e in body.edges}

    for eid in have_edges - want_edges:
        c.sb.table("flow_edges").delete().eq("edge_id", eid).execute()
    for nid in have_nodes - want_nodes:
        c.sb.table("flow_nodes").delete().eq("node_id", nid).execute()
    if body.nodes:
        c.sb.table("flow_nodes").upsert(
            [{"flow_id": flow_id, **n.model_dump()} for n in body.nodes]
        ).execute()
    if body.edges:
        c.sb.table("flow_edges").upsert(
            [{"flow_id": flow_id, **e.model_dump()} for e in body.edges]
        ).execute()
    c.sb.table("flows").update(
        {"name": body.name, "status": body.status, "version": body.version}
    ).eq("flow_id", flow_id).execute()

    return load_flow(flow_id=flow_id, sb=c.sb, validate=False)


@app.delete("/api/flows/{flow_id}", status_code=204)
def delete_flow(flow_id: str, c: Caller = Depends(caller)) -> None:
    _require_visible(c, flow_id)
    c.sb.table("flows").delete().eq("flow_id", flow_id).execute()  # cascades nodes/edges


@app.post("/api/flows/{flow_id}/run")
def run_flow(flow_id: str, body: RunIn, c: Caller = Depends(caller)) -> dict:
    _require_visible(c, flow_id)                       # RLS gate
    try:
        flow = load_flow(flow_id=flow_id, sb=_service, validate=True)
    except FlowInvalid as e:
        raise HTTPException(422, {"errors": e.errors})
    try:
        final = build_graph(flow).invoke({"case": body.case, "trace": []})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"run failed: {type(e).__name__}: {e}")
    return {
        "trace": final.get("trace", []),
        "outcome": final.get("outcome"),
        "tier": final.get("tier"),
        "region": final.get("region"),
        "confidence": final.get("confidence"),
        "confidence_gate": final.get("confidence_gate"),
        "sf_writeback": final.get("sf_writeback"),
        "query": final.get("query"),
        "retrieval": [
            {k: r.get(k) for k in ("doc_url", "heading_path", "rerank_score")}
            for r in final.get("retrieval", [])
        ],
    }
