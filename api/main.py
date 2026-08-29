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
from interpreter.loader import (  # noqa: E402
    FlowInvalid, FlowNotFound, definition_hash as flow_definition_hash, load_flow,
)
from interpreter import jobs  # noqa: E402
from interpreter.registry import known_types  # noqa: E402
from interpreter.runs import record_run  # noqa: E402

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
    "draft": {"model": "openai/gpt-oss-120b", "max_tokens": 500},
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
import time  # noqa: E402

import httpx  # noqa: E402

_token_cache: dict[str, tuple[float, str]] = {}   # token -> (expires_at, user_id)


def _verify_token(token: str) -> str:
    """Authoritative check — ask Supabase Auth. Verifies signature, expiry and
    revocation without needing the JWT secret. Cached 60s."""
    now = time.time()
    hit = _token_cache.get(token)
    if hit and hit[0] > now:
        return hit[1]
    try:
        r = httpx.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"Authorization": f"Bearer {token}", "apikey": ANON_KEY},
            timeout=5,
        )
    except httpx.HTTPError as e:
        raise HTTPException(503, f"auth check failed: {e}")
    if r.status_code != 200:
        raise HTTPException(401, "invalid or expired token")
    uid = r.json().get("id")
    if not uid:
        raise HTTPException(401, "token has no subject")
    _token_cache[token] = (now + 60, uid)
    # opportunistic cache prune
    if len(_token_cache) > 500:
        for k, (exp, _) in list(_token_cache.items()):
            if exp <= now:
                _token_cache.pop(k, None)
    return uid


class Caller:
    def __init__(self, token: str):
        self.token = token
        self.user_id = _verify_token(token)
        self.sb = create_client(SUPABASE_URL, ANON_KEY)
        self.sb.postgrest.auth(token)          # RLS applies to this client


def caller(authorization: str = Header(default="")) -> Caller:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    return Caller(authorization.split(" ", 1)[1].strip())


# ── rate limiting (per user, in-process token bucket) ──────────────────
_rate: dict[str, list[float]] = {}


def rate_limit(user_id: str, bucket: str, limit: int, window: float = 60.0) -> None:
    now = time.time()
    key = f"{user_id}:{bucket}"
    hits = [t for t in _rate.get(key, []) if now - t < window]
    if len(hits) >= limit:
        raise HTTPException(429, f"rate limit: {limit} {bucket}/{int(window)}s")
    hits.append(now)
    _rate[key] = hits


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
        .select("flow_id, tenant_id, team, name, status, version, published_version, updated_at")
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
    """The editable working draft (flow_nodes/flow_edges) + version pointers."""
    meta = _require_visible(c, flow_id)
    try:
        d = load_flow(flow_id=flow_id, sb=c.sb, status="draft", validate=False)
    except FlowNotFound:
        raise HTTPException(404, "flow not found")
    d["published_version"] = meta.get("published_version")
    return d


@app.post("/api/flows/{flow_id}/validate")
def validate_flow_ep(flow_id: str, body: FlowIn, c: Caller = Depends(caller)) -> dict:
    meta = _require_visible(c, flow_id)
    errs = _structural_errors(_flow_dict(meta, body))
    return {"valid": not errs, "errors": errs}


@app.put("/api/flows/{flow_id}")
def save_flow(flow_id: str, body: FlowIn, c: Caller = Depends(caller)) -> dict:
    """Save the working draft — one transactional RPC. Optimistic concurrency:
    `body.version` must match the flow's current `version` or it's a 409."""
    meta = _require_visible(c, flow_id)
    if body.version != meta["version"]:
        raise HTTPException(409, {
            "message": "flow changed since you loaded it — reload",
            "your_version": body.version, "current_version": meta["version"],
        })
    errs = _structural_errors(_flow_dict(meta, body))
    if errs:
        raise HTTPException(422, {"errors": errs})

    c.sb.rpc("replace_flow_graph", {
        "p_flow_id": flow_id,
        "p_nodes": [n.model_dump() for n in body.nodes],
        "p_edges": [e.model_dump() for e in body.edges],
    }).execute()
    new_version = meta["version"] + 1
    status = body.status if body.status in ("draft", "archived") else meta["status"]
    c.sb.table("flows").update(
        {"name": body.name, "status": status, "version": new_version}
    ).eq("flow_id", flow_id).execute()

    out = load_flow(flow_id=flow_id, sb=c.sb, status="draft", validate=False)
    out["published_version"] = meta.get("published_version")
    return out


@app.get("/api/flows/{flow_id}/versions")
def list_versions(flow_id: str, c: Caller = Depends(caller)) -> list[dict]:
    _require_visible(c, flow_id)
    return (
        c.sb.table("flow_versions")
        .select("version, name, definition_hash, created_by, created_at")
        .eq("flow_id", flow_id).order("version", desc=True).execute().data
        or []
    )


@app.post("/api/flows/{flow_id}/publish")
def publish_flow(flow_id: str, c: Caller = Depends(caller)) -> dict:
    """Snapshot the current draft into an immutable flow_versions row and
    point `published_version` at it."""
    meta = _require_visible(c, flow_id)
    draft = load_flow(flow_id=flow_id, sb=c.sb, status="draft", validate=False)
    errs = _structural_errors(draft)
    if errs:
        raise HTTPException(422, {"errors": errs})

    prev = (
        c.sb.table("flow_versions").select("version")
        .eq("flow_id", flow_id).order("version", desc=True).limit(1).execute().data
    )
    version = (prev[0]["version"] + 1) if prev else 1
    c.sb.table("flow_versions").insert({
        "flow_id": flow_id, "version": version, "name": draft["name"],
        "nodes": draft["nodes"], "edges": draft["edges"],
        "definition_hash": flow_definition_hash(draft["nodes"], draft["edges"]),
        "created_by": c.user_id,
    }).execute()
    c.sb.table("flows").update({
        "status": "published", "published_version": version,
        "version": meta["version"] + 1,
    }).eq("flow_id", flow_id).execute()
    return {"published_version": version}


class RollbackIn(BaseModel):
    version: int


@app.post("/api/flows/{flow_id}/rollback")
def rollback_flow(flow_id: str, body: RollbackIn, c: Caller = Depends(caller)) -> dict:
    """Restore the working draft from an old snapshot and re-publish it."""
    meta = _require_visible(c, flow_id)
    snap = (
        c.sb.table("flow_versions").select("version, nodes, edges")
        .eq("flow_id", flow_id).eq("version", body.version).execute().data
    )
    if not snap:
        raise HTTPException(404, f"no version {body.version}")
    c.sb.rpc("replace_flow_graph", {
        "p_flow_id": flow_id,
        "p_nodes": snap[0]["nodes"], "p_edges": snap[0]["edges"],
    }).execute()
    c.sb.table("flows").update({
        "published_version": body.version, "version": meta["version"] + 1,
    }).eq("flow_id", flow_id).execute()
    return {"published_version": body.version}


@app.delete("/api/flows/{flow_id}", status_code=204)
def delete_flow(flow_id: str, c: Caller = Depends(caller)) -> None:
    _require_visible(c, flow_id)
    c.sb.table("flows").delete().eq("flow_id", flow_id).execute()  # cascades nodes/edges


@app.post("/api/flows/{flow_id}/run")
def run_flow(
    flow_id: str,
    body: RunIn,
    c: Caller = Depends(caller),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    rate_limit(c.user_id, "run", 20)
    _require_visible(c, flow_id)                       # RLS gate

    if idempotency_key:
        dup = (
            _service.table("runs").select("run_id")
            .eq("flow_id", flow_id).eq("idempotency_key", idempotency_key)
            .execute().data
        )
        if dup:
            return {"run_id": dup[0]["run_id"], "idempotent_replay": True}

    try:
        flow = load_flow(flow_id=flow_id, sb=_service, validate=True)
    except FlowInvalid as e:
        raise HTTPException(422, {"errors": e.errors})
    try:
        final = build_graph(flow).invoke({"case": body.case, "tenant_id": flow["tenant_id"], "trace": []})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"run failed: {type(e).__name__}: {e}")

    run_id = record_run(flow, final, case=body.case, source="api",
                        idempotency_key=idempotency_key, sb=_service)

    return {
        "run_id": run_id,
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


class EnqueueIn(BaseModel):
    case: dict[str, Any]
    idempotency_key: str | None = None


@app.post("/api/flows/{flow_id}/enqueue", status_code=202)
def enqueue_run(flow_id: str, body: EnqueueIn, c: Caller = Depends(caller)) -> dict:
    """Queue a run for the worker (async path — used by the Salesforce trigger).
    Returns immediately. `GET /jobs/{job_id}` for status; the result carries the
    `run_id`."""
    rate_limit(c.user_id, "enqueue", 120)
    _require_visible(c, flow_id)
    job_id = jobs.enqueue(
        "run_flow",
        {"flow_id": flow_id, "case": body.case, "idempotency_key": body.idempotency_key},
        dedupe_key=body.idempotency_key,
        sb=_service,
    )
    if job_id is None:
        return {"job_id": None, "deduped": True}
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, c: Caller = Depends(caller)) -> dict:
    rows = (
        _service.table("jobs")
        .select("job_id, kind, status, attempts, payload, result, error, created_at, updated_at")
        .eq("job_id", job_id).execute().data
    )
    if not rows:
        raise HTTPException(404, "job not found")
    job = rows[0]
    fid = (job.get("payload") or {}).get("flow_id")
    if fid:
        _require_visible(c, fid)          # only see jobs for flows in your tenant
    job.pop("payload", None)
    return job


# ── runs (Phase 6 observability) ──────────────────────────────────────
@app.get("/api/runs/stats")
def runs_stats(c: Caller = Depends(caller)) -> dict:
    rows = (
        c.sb.table("runs")
        .select("outcome, tier, team, confidence, human_action, created_at")
        .order("created_at", desc=True)
        .limit(500)
        .execute().data
        or []
    )
    by_outcome: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    by_human: dict[str, int] = {}
    low = 0
    for r in rows:
        by_outcome[r.get("outcome") or "?"] = by_outcome.get(r.get("outcome") or "?", 0) + 1
        by_tier[r.get("tier") or "?"] = by_tier.get(r.get("tier") or "?", 0) + 1
        if r.get("human_action"):
            by_human[r["human_action"]] = by_human.get(r["human_action"], 0) + 1
        if (r.get("confidence") is not None) and float(r["confidence"]) < 0.4:
            low += 1
    resolved = sum(v for k, v in by_human.items() if k != "pending")
    kept = by_human.get("sent_as_is", 0) + by_human.get("edited", 0)
    return {
        "total": len(rows), "by_outcome": by_outcome, "by_tier": by_tier,
        "low_confidence": low, "by_human_action": by_human,
        "draft_acceptance": round(kept / resolved, 3) if resolved else None,
    }


@app.get("/api/runs")
def list_runs(
    flow_id: str | None = None,
    outcome: str | None = None,
    limit: int = 60,
    c: Caller = Depends(caller),
) -> list[dict]:
    q = (
        c.sb.table("runs")
        .select("run_id, flow_id, team, tier, region, outcome, confidence, subject, "
                "source, human_action, edit_distance, created_at")
        .order("created_at", desc=True)
        .limit(min(max(limit, 1), 200))
    )
    if flow_id:
        q = q.eq("flow_id", flow_id)
    if outcome:
        q = q.eq("outcome", outcome)
    return q.execute().data or []


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, c: Caller = Depends(caller)) -> dict:
    rows = c.sb.table("runs").select("*").eq("run_id", run_id).execute().data or []
    if not rows:
        raise HTTPException(404, "run not found")
    return rows[0]
