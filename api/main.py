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
import secrets
import uuid
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
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
from interpreter import gdrive, github as githubmod, slack as slackmod  # noqa: E402
from ingestion.sources.kb_common import delete_entry as _kb_delete, embed_entry as _kb_embed  # noqa: E402

import hashlib  # noqa: E402

# markdown bodies below this size are chunked + embedded inline in the
# request; larger ones are handed to the worker (`embed_kb_entry` job).
KB_INLINE_EMBED_MAX = 8192

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
    "kb_lookup": {"collections": [], "top_k": 4, "use_rerank": True,
                  "min_score": 0.0, "out_key": "internal_kb"},
    "extract": {"fields": {}},
    "policy_gate": {},
    "task_dispatch": {},
    "draft": {"model": "openai/gpt-oss-120b", "max_tokens": 900},
    "confidence_gate": {
        "default_threshold": 0.5,
        "tier_overrides": {"basic": 0.5, "premium": 0.55, "enterprise": 0.6},
        "weights": {"retrieval": 0.55, "draft": 0.1, "groundedness": 0.35},
        "escalate_topics": ["billing", "refund", "pricing", "compliance", "legal",
                            "account-access", "data-export", "partner-api", "cancellation"],
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


class KbCollectionIn(BaseModel):
    name: str
    description: str | None = None
    tenant_id: str | None = None      # required only if the caller is in >1 tenant


class KbCollectionPatch(BaseModel):
    name: str | None = None
    description: str | None = None


class KbEntryIn(BaseModel):
    title: str
    body_md: str = ""


class KbEntryPatch(BaseModel):
    title: str | None = None
    body_md: str | None = None


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
        final = build_graph(flow).invoke({"case": body.case, "tenant_id": flow["tenant_id"], "team": flow.get("team"), "trace": []})
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


# ── Phase 14: self-serve internal knowledge base ──────────────────────
def _caller_tenant(c: Caller, explicit: str | None) -> str:
    """RLS lets a member read only their own tenant_members rows."""
    mine = [r["tenant_id"] for r in
            (c.sb.table("tenant_members").select("tenant_id").execute().data or [])]
    if explicit:
        if explicit not in mine:
            raise HTTPException(403, "not a member of that tenant")
        return explicit
    if len(mine) == 1:
        return mine[0]
    raise HTTPException(400, "tenant_id required (you belong to several tenants)")


def _kb_collection(c: Caller, sid: str) -> dict:
    rows = (c.sb.table("sources").select("*")
            .eq("source_id", sid).eq("kind", "internal_kb").execute().data or [])
    if not rows:
        raise HTTPException(404, "collection not found or not visible to you")
    return rows[0]


def _kb_entry(c: Caller, eid: str) -> dict:
    rows = (c.sb.table("kb_entries").select("*")
            .eq("entry_id", eid).neq("status", "archived").execute().data or [])
    if not rows:
        raise HTTPException(404, "entry not found or not visible to you")
    return rows[0]


def _kb_url(sid: str, eid: str) -> str:
    return f"kb://{sid}/{eid}"


def _kb_embed_now(entry: dict, collection_name: str) -> dict:
    """Chunk + embed one entry's markdown into the shared content tables
    (service role) and stamp the kb_entries row. Returns the updated row."""
    url = _kb_url(entry["source_id"], entry["entry_id"])
    n = _kb_embed(_service, source_id=entry["source_id"], url=url,
                  title=entry["title"], body_md=entry["body_md"] or "",
                  section=collection_name)
    patch = {
        "chunk_count": n,
        "embed_hash": hashlib.md5((entry["body_md"] or "").encode()).hexdigest(),
        "embedded_at": _now_iso(),
    }
    _service.table("kb_entries").update(patch).eq("entry_id", entry["entry_id"]).execute()
    return {**entry, **patch}


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@app.get("/api/kb/collections")
def kb_list_collections(c: Caller = Depends(caller)) -> list[dict]:
    cols = (c.sb.table("sources").select("*")
            .eq("kind", "internal_kb").neq("status", "archived").execute().data or [])
    out = []
    for s in cols:
        entries = (c.sb.table("kb_entries").select("entry_id, status")
                   .eq("source_id", s["source_id"]).execute().data or [])
        active = [e for e in entries if e["status"] == "active"]
        out.append({
            "source_id": s["source_id"], "name": s["name"],
            "description": (s.get("config") or {}).get("description"),
            "tenant_id": s["tenant_id"], "entry_count": len(active),
            "created_at": s.get("created_at"),
        })
    return out


@app.post("/api/kb/collections", status_code=201)
def kb_create_collection(body: KbCollectionIn, c: Caller = Depends(caller)) -> dict:
    rate_limit(c.user_id, "kb_write", 60)
    tenant_id = _caller_tenant(c, body.tenant_id)
    row = {
        "kind": "internal_kb", "tenant_id": tenant_id, "name": body.name,
        "config": {"description": body.description} if body.description else {},
    }
    try:
        created = c.sb.table("sources").insert(row).execute().data[0]
    except Exception as e:  # noqa: BLE001  (unique (tenant_id, name) etc.)
        raise HTTPException(409, f"could not create collection: {e}")
    return created


@app.patch("/api/kb/collections/{sid}")
def kb_update_collection(sid: str, body: KbCollectionPatch, c: Caller = Depends(caller)) -> dict:
    rate_limit(c.user_id, "kb_write", 60)
    col = _kb_collection(c, sid)
    patch: dict[str, Any] = {}
    if body.name is not None:
        patch["name"] = body.name
    if body.description is not None:
        patch["config"] = {**(col.get("config") or {}), "description": body.description}
    if not patch:
        return col
    return c.sb.table("sources").update(patch).eq("source_id", sid).execute().data[0]


@app.delete("/api/kb/collections/{sid}", status_code=204)
def kb_delete_collection(sid: str, c: Caller = Depends(caller)) -> None:
    rate_limit(c.user_id, "kb_write", 60)
    _kb_collection(c, sid)                     # RLS gate
    c.sb.table("sources").update({"status": "archived"}).eq("source_id", sid).execute()
    entries = (c.sb.table("kb_entries").select("entry_id")
               .eq("source_id", sid).eq("status", "active").execute().data or [])
    for e in entries:
        c.sb.table("kb_entries").update({"status": "archived"}).eq("entry_id", e["entry_id"]).execute()
        _kb_delete(_service, url=_kb_url(sid, e["entry_id"]))


@app.get("/api/kb/collections/{sid}/entries")
def kb_list_entries(sid: str, c: Caller = Depends(caller)) -> list[dict]:
    _kb_collection(c, sid)
    rows = (c.sb.table("kb_entries")
            .select("entry_id, title, status, chunk_count, embedded_at, updated_at, updated_by")
            .eq("source_id", sid).neq("status", "archived")
            .order("updated_at", desc=True).execute().data or [])
    return rows


@app.post("/api/kb/collections/{sid}/entries", status_code=201)
def kb_create_entry(sid: str, body: KbEntryIn, c: Caller = Depends(caller)) -> dict:
    rate_limit(c.user_id, "kb_write", 60)
    col = _kb_collection(c, sid)
    row = {
        "source_id": sid, "tenant_id": col["tenant_id"], "title": body.title,
        "body_md": body.body_md, "created_by": c.user_id, "updated_by": c.user_id,
    }
    entry = c.sb.table("kb_entries").insert(row).execute().data[0]
    return _kb_after_write(entry, col, c)


@app.get("/api/kb/entries/{eid}")
def kb_get_entry(eid: str, c: Caller = Depends(caller)) -> dict:
    return _kb_entry(c, eid)


@app.patch("/api/kb/entries/{eid}")
def kb_update_entry(eid: str, body: KbEntryPatch, c: Caller = Depends(caller)) -> dict:
    rate_limit(c.user_id, "kb_write", 60)
    entry = _kb_entry(c, eid)
    if entry.get("origin") == "gdoc" and body.body_md is not None:
        raise HTTPException(409, "this entry is synced from Google Docs — edit the doc, then re-sync")
    col = _kb_collection(c, entry["source_id"])
    patch: dict[str, Any] = {"updated_by": c.user_id}
    if body.title is not None:
        patch["title"] = body.title
    if body.body_md is not None:
        patch["body_md"] = body.body_md
    updated = c.sb.table("kb_entries").update(patch).eq("entry_id", eid).execute().data[0]
    body_changed = body.body_md is not None and (
        hashlib.md5((body.body_md or "").encode()).hexdigest() != (entry.get("embed_hash") or "")
    )
    return _kb_after_write(updated, col, c, force=body_changed, title_only=not body_changed)


@app.delete("/api/kb/entries/{eid}", status_code=204)
def kb_delete_entry(eid: str, c: Caller = Depends(caller)) -> None:
    rate_limit(c.user_id, "kb_write", 60)
    entry = _kb_entry(c, eid)
    c.sb.table("kb_entries").update({"status": "archived", "updated_by": c.user_id}) \
        .eq("entry_id", eid).execute()
    _kb_delete(_service, url=_kb_url(entry["source_id"], eid))


def _kb_after_write(entry: dict, col: dict, c: Caller, *, force: bool = True,
                    title_only: bool = False) -> dict:
    """Embed the entry now (small) or hand it to the worker (large).
    `title_only` PATCHes just re-stamp the doc title without re-chunking."""
    body = entry.get("body_md") or ""
    if title_only:
        _service.table("zapier_docs").update({"title": entry["title"]}) \
            .eq("url", _kb_url(entry["source_id"], entry["entry_id"])).execute()
        return entry
    if not force:
        return entry
    if len(body) <= KB_INLINE_EMBED_MAX:
        return _kb_embed_now(entry, col["name"])
    jobs.enqueue("embed_kb_entry",
                 {"entry_id": entry["entry_id"], "source_id": entry["source_id"],
                  "collection_name": col["name"]},
                 dedupe_key=f"embed:{entry['entry_id']}", sb=_service)
    return {**entry, "embed_status": "queued"}


# ── Phase 15: Google Docs connector ──────────────────────────────────
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI", "http://localhost:8000/api/integrations/google/callback"
)
# short-lived OAuth state: nonce -> (expires_at, user_id, tenant_id)
_oauth_state: dict[str, tuple[float, str, str]] = {}


class GdocLinkIn(BaseModel):
    doc_url: str
    tenant_id: str | None = None


@app.get("/api/integrations/google/status")
def google_status(c: Caller = Depends(caller)) -> dict:
    tenants = [r["tenant_id"] for r in
               (c.sb.table("tenant_members").select("tenant_id").execute().data or [])]
    return {
        "configured": gdrive.available(),
        "connected": {t: gdrive.connected(t, _service) for t in tenants},
    }


@app.get("/api/integrations/google/authorize")
def google_authorize(tenant_id: str | None = None, c: Caller = Depends(caller)) -> dict:
    if not gdrive.available():
        raise HTTPException(503, "Google is not configured on this server")
    tid = _caller_tenant(c, tenant_id)
    nonce = secrets.token_urlsafe(24)
    _oauth_state[nonce] = (time.time() + 600, c.user_id, tid)
    return {"url": gdrive.authorize_url(GOOGLE_REDIRECT_URI, nonce)}


@app.get("/api/integrations/google/callback")
def google_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    def page(msg: str) -> HTMLResponse:
        return HTMLResponse(
            f"<!doctype html><meta charset=utf-8><p>{msg}</p>"
            "<script>setTimeout(()=>window.close(),1500)</script>"
        )
    if error:
        return page(f"Google authorisation failed: {error}")
    hit = _oauth_state.pop(state, None)
    if not hit or hit[0] < time.time():
        return page("This authorisation link has expired — try again.")
    _, _user_id, tenant_id = hit
    try:
        tok = gdrive.exchange_code(code, GOOGLE_REDIRECT_URI)
    except Exception as e:  # noqa: BLE001
        return page(f"Could not complete Google sign-in: {e}")
    _service.table("tenant_integrations").upsert({
        "tenant_id": tenant_id, "kind": "google",
        "secret": {"refresh_token": tok["refresh_token"], "scope": tok.get("scope")},
    }).execute()
    return page("Google connected. You can close this window.")


@app.post("/api/kb/collections/{sid}/gdoc", status_code=201)
def kb_link_gdoc(sid: str, body: GdocLinkIn, c: Caller = Depends(caller)) -> dict:
    rate_limit(c.user_id, "kb_write", 60)
    col = _kb_collection(c, sid)
    if not gdrive.connected(col["tenant_id"], _service):
        raise HTTPException(400, "connect Google for this tenant first")
    try:
        doc_id = gdrive.parse_doc_id(body.doc_url)
        fetched = gdrive.fetch_doc(col["tenant_id"], doc_id, _service)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Google fetch failed: {e}")

    existing = (c.sb.table("kb_entries").select("entry_id")
                .eq("source_id", sid).eq("gdoc_id", doc_id).neq("status", "archived")
                .execute().data or [])
    row = {
        "source_id": sid, "tenant_id": col["tenant_id"], "title": fetched["title"],
        "body_md": fetched["markdown"], "origin": "gdoc", "gdoc_id": doc_id,
        "gdoc_url": body.doc_url.strip(), "gdoc_modified": fetched["modified_time"],
        "synced_at": _now_iso(), "sync_error": None,
        "created_by": c.user_id, "updated_by": c.user_id,
    }
    if existing:
        eid = existing[0]["entry_id"]
        entry = c.sb.table("kb_entries").update(row).eq("entry_id", eid).execute().data[0]
    else:
        entry = c.sb.table("kb_entries").insert(row).execute().data[0]
    return _kb_after_write(entry, col, c)


@app.post("/api/kb/entries/{eid}/resync")
def kb_resync_gdoc(eid: str, c: Caller = Depends(caller)) -> dict:
    rate_limit(c.user_id, "kb_write", 60)
    entry = _kb_entry(c, eid)
    if entry.get("origin") != "gdoc":
        raise HTTPException(400, "not a Google-linked entry")
    col = _kb_collection(c, entry["source_id"])
    try:
        fetched = gdrive.fetch_doc(col["tenant_id"], entry["gdoc_id"], _service)
    except Exception as e:  # noqa: BLE001
        c.sb.table("kb_entries").update({"sync_error": str(e)[:500]}).eq("entry_id", eid).execute()
        raise HTTPException(502, f"Google fetch failed: {e}")
    updated = c.sb.table("kb_entries").update({
        "title": fetched["title"], "body_md": fetched["markdown"],
        "gdoc_modified": fetched["modified_time"], "synced_at": _now_iso(),
        "sync_error": None, "updated_by": c.user_id,
    }).eq("entry_id", eid).execute().data[0]
    return _kb_after_write(updated, col, c)


# ── Phase 16: policy rules ───────────────────────────────────────────
class RuleIn(BaseModel):
    team: str
    name: str
    priority: int = 100
    when: dict[str, Any] = {}
    then: dict[str, Any] = {}
    status: str = "active"
    tenant_id: str | None = None


class RulePatch(BaseModel):
    name: str | None = None
    priority: int | None = None
    when: dict[str, Any] | None = None
    then: dict[str, Any] | None = None
    status: str | None = None


@app.get("/api/rules")
def list_rules(team: str | None = None, c: Caller = Depends(caller)) -> list[dict]:
    q = c.sb.table("policy_rules").select("*")
    if team:
        q = q.eq("team", team)
    return q.order("priority").execute().data or []


@app.post("/api/rules", status_code=201)
def create_rule(body: RuleIn, c: Caller = Depends(caller)) -> dict:
    rate_limit(c.user_id, "rules_write", 60)
    tenant_id = _caller_tenant(c, body.tenant_id)
    row = {
        "tenant_id": tenant_id, "team": body.team, "name": body.name,
        "priority": body.priority, "when": body.when, "then": body.then,
        "status": body.status, "created_by": c.user_id, "updated_by": c.user_id,
    }
    try:
        return c.sb.table("policy_rules").insert(row).execute().data[0]
    except Exception as e:  # noqa: BLE001
        raise HTTPException(409, f"could not create rule: {e}")


@app.patch("/api/rules/{rule_id}")
def update_rule(rule_id: str, body: RulePatch, c: Caller = Depends(caller)) -> dict:
    rate_limit(c.user_id, "rules_write", 60)
    cur = c.sb.table("policy_rules").select("rule_id").eq("rule_id", rule_id).execute().data
    if not cur:
        raise HTTPException(404, "rule not found or not visible to you")
    patch = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    patch["updated_by"] = c.user_id
    return c.sb.table("policy_rules").update(patch).eq("rule_id", rule_id).execute().data[0]


@app.delete("/api/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: str, c: Caller = Depends(caller)) -> None:
    rate_limit(c.user_id, "rules_write", 60)
    c.sb.table("policy_rules").delete().eq("rule_id", rule_id).execute()


@app.get("/api/action-requests")
def list_action_requests(limit: int = 50, c: Caller = Depends(caller)) -> list[dict]:
    return (c.sb.table("action_requests").select("*")
            .order("created_at", desc=True).limit(min(limit, 200)).execute().data or [])


# ── Phase 16: Slack integration ─────────────────────────────────────
SLACK_REDIRECT_URI = os.environ.get(
    "SLACK_REDIRECT_URI", "http://localhost:8000/api/integrations/slack/callback"
)


@app.get("/api/integrations/slack/status")
def slack_status(c: Caller = Depends(caller)) -> dict:
    tenants = [r["tenant_id"] for r in
               (c.sb.table("tenant_members").select("tenant_id").execute().data or [])]
    return {"configured": slackmod.available(),
            "connected": {t: slackmod.connected(t, _service) for t in tenants}}


@app.get("/api/integrations/slack/authorize")
def slack_authorize(tenant_id: str | None = None, c: Caller = Depends(caller)) -> dict:
    if not slackmod.available():
        raise HTTPException(503, "Slack is not configured on this server")
    tid = _caller_tenant(c, tenant_id)
    nonce = secrets.token_urlsafe(24)
    _oauth_state[nonce] = (time.time() + 600, c.user_id, tid)
    return {"url": slackmod.authorize_url(SLACK_REDIRECT_URI, nonce)}


@app.get("/api/integrations/slack/callback")
def slack_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    def page(msg: str) -> HTMLResponse:
        return HTMLResponse(f"<!doctype html><meta charset=utf-8><p>{msg}</p>"
                            "<script>setTimeout(()=>window.close(),1500)</script>")
    if error:
        return page(f"Slack authorisation failed: {error}")
    hit = _oauth_state.pop(state, None)
    if not hit or hit[0] < time.time():
        return page("This authorisation link has expired — try again.")
    _, _uid, tenant_id = hit
    try:
        tok = slackmod.exchange_code(code, SLACK_REDIRECT_URI)
    except Exception as e:  # noqa: BLE001
        return page(f"Could not complete Slack install: {e}")
    _service.table("tenant_integrations").upsert({
        "tenant_id": tenant_id, "kind": "slack",
        "secret": {"bot_token": tok["bot_token"], "team": tok.get("team"),
                   "bot_user_id": tok.get("bot_user_id")},
    }).execute()
    return page("Slack connected. You can close this window.")


@app.post("/api/integrations/slack/interactions")
async def slack_interactions(request: Request) -> PlainTextResponse:
    raw = await request.body()
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not secret or not slackmod.verify_signature(
        secret, request.headers.get("X-Slack-Request-Timestamp", ""),
        raw, request.headers.get("X-Slack-Signature", ""),
    ):
        raise HTTPException(401, "bad slack signature")

    from urllib.parse import parse_qs
    import json as _json
    payload = _json.loads(parse_qs(raw.decode())["payload"][0])
    action = (payload.get("actions") or [{}])[0]
    ar_id = action.get("value")
    decision = action.get("action_id")            # 'approve' | 'reject'
    user = (payload.get("user") or {}).get("username") or (payload.get("user") or {}).get("id")
    if not ar_id or decision not in ("approve", "reject"):
        return PlainTextResponse("ignored")

    rows = _service.table("action_requests").select("*").eq("id", ar_id).execute().data
    if not rows:
        return PlainTextResponse("unknown request")
    ar = rows[0]
    if ar["status"] != "pending":
        return PlainTextResponse(f"already {ar['status']}")

    new_status = "approved" if decision == "approve" else "rejected"
    _service.table("action_requests").update({
        "status": new_status, "decided_by": user, "decided_at": _now_iso(),
    }).eq("id", ar_id).execute()

    if new_status == "approved":
        jobs.enqueue("create_github_issue", {"action_request_id": ar_id},
                     dedupe_key=f"ghissue:{ar_id}", sb=_service)
        msg = f":hourglass_flowing_sand: *{ar['payload'].get('title')}* — approved by {user}, opening the issue…"
    else:
        msg = f":no_entry: *{ar['payload'].get('title')}* — rejected by {user}."
    try:
        if ar.get("slack_channel") and ar.get("slack_ts"):
            slackmod.update_message(ar["tenant_id"], ar["slack_channel"], ar["slack_ts"], msg, _service)
    except Exception:  # noqa: BLE001
        pass
    return PlainTextResponse("ok")
