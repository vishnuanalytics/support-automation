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

import json
import logging
import os
import secrets
import uuid
from datetime import datetime as _dt
from typing import Any

log = logging.getLogger("api")

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
from interpreter import jobs, sf_ingest  # noqa: E402
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
    "team_route": {"default": "support"},
    "case_lookup": {"k": 3, "pool": 10, "min_similarity": 0.35},
    "notify": {"channel": "salesforce_chatter", "target_by_type": {}, "fallback_target": None},
    "clarify": {"max_questions": 3, "max_rounds": 2, "auto_send": False, "channel": "email"},
    # Phase 24 — every path ends here: tag the agent + open the Slack reasoning
    # dialogue; the customer reply is drafted and sent only on the agent's OK.
    "notify_human": {
        "channel": "both",
        "slack_channel": "#support-escalations",
        "max_rounds": 3,
        "mention": {},
    },
}


# ── auth ───────────────────────────────────────────────────────────────
import time  # noqa: E402

import httpx  # noqa: E402

_token_cache: dict[str, tuple[float, str, str | None]] = {}   # token -> (expires_at, user_id, email)


def _verify_token(token: str) -> tuple[str, str | None]:
    """Authoritative check — ask Supabase Auth. Verifies signature, expiry and
    revocation without needing the JWT secret. Cached 60s. Returns (user_id, email)."""
    now = time.time()
    hit = _token_cache.get(token)
    if hit and hit[0] > now:
        return hit[1], hit[2]
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
    body = r.json()
    uid = body.get("id")
    if not uid:
        raise HTTPException(401, "token has no subject")
    email = (body.get("email") or "").lower() or None
    _token_cache[token] = (now + 60, uid, email)
    # opportunistic cache prune
    if len(_token_cache) > 500:
        for k, (exp, *_rest) in list(_token_cache.items()):
            if exp <= now:
                _token_cache.pop(k, None)
    return uid, email


class Caller:
    def __init__(self, token: str):
        self.token = token
        self.user_id, self.email = _verify_token(token)
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
    team: str
    name: str
    status: str = "draft"
    tenant_id: str | None = None   # optional — inferred from the caller's membership


class RunIn(BaseModel):
    case: dict[str, Any]


class MermaidIn(BaseModel):
    text: str
    tenant_id: str | None = None      # only needed if the caller is in >1 tenant


class AssistIn(BaseModel):
    prompt: str
    tenant_id: str | None = None


class AssistEditIn(BaseModel):
    instruction: str


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


def _member_role(c: Caller, tenant_id: str) -> str | None:
    rows = (c.sb.table("tenant_members").select("role")
            .eq("user_id", c.user_id).eq("tenant_id", tenant_id).execute().data or [])
    return rows[0].get("role") if rows else None


def _require_editor(c: Caller, tenant_id: str) -> None:
    """Phase 18b — a clean 403 for view-only members before a write.
    RLS is the real backstop; this just avoids a raw Postgres error string."""
    role = _member_role(c, tenant_id)
    if role is None:
        raise HTTPException(403, "not a member of that tenant")
    if role not in ("owner", "editor"):
        raise HTTPException(403, "your access is view-only")


def _require_owner(c: Caller, tenant_id: str) -> None:
    """Phase 18c — only an owner manages members / invitations."""
    if _member_role(c, tenant_id) != "owner":
        raise HTTPException(403, "only a workspace owner can do that")


# ── endpoints ──────────────────────────────────────────────────────────
@app.get("/api/health")
def health() -> dict:
    """Liveness + the last heartbeat age (seconds) of each pipeline component,
    so one URL covers the whole stack for an uptime monitor."""
    import time as _t

    out: dict = {"ok": True, "components": {}}
    try:
        rows = _service.table("system_health").select("component,last_healthy_at").execute().data or []
        now = _t.time()
        for r in rows:
            try:
                ts = _dt.fromisoformat(str(r["last_healthy_at"]).replace("Z", "+00:00")).timestamp()
                out["components"][r["component"]] = round(now - ts, 1)
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        out["components_error"] = str(e)
    return out


@app.get("/api/node-types")
def node_types() -> dict:
    return {"types": sorted(known_types()), "defaults": NODE_DEFAULTS}


_SF_META_CACHE: dict = {"at": 0.0, "data": None}


@app.get("/api/salesforce/meta")
def salesforce_meta(c: Caller = Depends(caller)) -> dict:
    """Salesforce routing queues + the Case.Type / Module__c picklists, for the
    flow editor's dropdowns (notify / clarify node forms). Cached 5 min in
    process. `available:false` + empty lists when the API has no SF creds."""
    import time

    from interpreter import salesforce as _sf

    now = time.time()
    if _SF_META_CACHE["data"] is None or now - _SF_META_CACHE["at"] > 300:
        _SF_META_CACHE["data"] = _sf.org_metadata()
        _SF_META_CACHE["at"] = now
    return _SF_META_CACHE["data"]


@app.get("/api/flows")
def list_flows(c: Caller = Depends(caller)) -> list[dict]:
    rows = (
        c.sb.table("flows")
        .select("flow_id, tenant_id, team, name, status, version, published_version, sf_entry, updated_at")
        .order("tenant_id").order("team").execute().data
        or []
    )
    return rows


@app.get("/api/tenants")
def list_tenants(c: Caller = Depends(caller)) -> list[dict]:
    """The caller's tenant memberships — the UI uses this to pick a tenant
    for a new flow (or skip the prompt when there's exactly one)."""
    return (c.sb.table("tenant_members").select("tenant_id, role")
            .eq("user_id", c.user_id).execute().data or [])


# ── Phase 18c: team invitations ──────────────────────────────────────
class InviteIn(BaseModel):
    email: str
    role: str = "viewer"          # 'editor' | 'viewer' (never 'owner' via invite)
    tenant_id: str | None = None


def _emails_for(user_ids: list[str]) -> dict[str, str]:
    """Best-effort user_id -> email via the Auth admin API (service role)."""
    out: dict[str, str] = {}
    for uid in set(user_ids):
        try:
            u = _service.auth.admin.get_user_by_id(uid)
            out[uid] = getattr(u.user, "email", None) or ""
        except Exception:  # noqa: BLE001
            out[uid] = ""
    return out


@app.get("/api/members")
def list_members(tenant_id: str | None = None, c: Caller = Depends(caller)) -> list[dict]:
    tid = _caller_tenant(c, tenant_id)
    _require_owner(c, tid)
    rows = (_service.table("tenant_members").select("user_id, role")
            .eq("tenant_id", tid).execute().data or [])
    emails = _emails_for([r["user_id"] for r in rows])
    return [{**r, "email": emails.get(r["user_id"], ""), "is_you": r["user_id"] == c.user_id}
            for r in rows]


@app.delete("/api/members/{user_id}", status_code=204)
def remove_member(user_id: str, tenant_id: str | None = None, c: Caller = Depends(caller)) -> None:
    tid = _caller_tenant(c, tenant_id)
    _require_owner(c, tid)
    if user_id == c.user_id:
        raise HTTPException(400, "you can't remove yourself")
    owners = (_service.table("tenant_members").select("user_id")
              .eq("tenant_id", tid).eq("role", "owner").execute().data or [])
    if len(owners) <= 1 and any(o["user_id"] == user_id for o in owners):
        raise HTTPException(400, "can't remove the last owner")
    _service.table("tenant_members").delete() \
        .eq("tenant_id", tid).eq("user_id", user_id).execute()


@app.get("/api/invitations")
def list_invitations(c: Caller = Depends(caller)) -> list[dict]:
    """RLS: an owner sees their tenant's rows; an invitee sees their own pending ones."""
    return (c.sb.table("tenant_invitations").select("*")
            .order("created_at", desc=True).execute().data or [])


@app.post("/api/invitations", status_code=201)
def create_invitation(body: InviteIn, c: Caller = Depends(caller)) -> dict:
    tid = _caller_tenant(c, body.tenant_id)
    _require_owner(c, tid)
    role = body.role.strip().lower()
    if role not in ("editor", "viewer"):
        raise HTTPException(400, "role must be 'editor' or 'viewer'")
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "a real email is required")
    try:
        return c.sb.table("tenant_invitations").insert({
            "tenant_id": tid, "email": email, "role": role, "invited_by": c.user_id,
        }).execute().data[0]
    except Exception as e:  # noqa: BLE001  — dup pending invite, etc.
        raise HTTPException(409, f"could not invite {email}: {e}")


@app.delete("/api/invitations/{invite_id}", status_code=204)
def revoke_invitation(invite_id: str, c: Caller = Depends(caller)) -> None:
    cur = (c.sb.table("tenant_invitations").select("tenant_id")
           .eq("invite_id", invite_id).execute().data)
    if cur:
        _require_owner(c, cur[0]["tenant_id"])
    c.sb.table("tenant_invitations").update({"status": "revoked"}) \
        .eq("invite_id", invite_id).execute()


@app.post("/api/invitations/accept")
def accept_invitations(c: Caller = Depends(caller)) -> dict:
    """Claim every pending invite for the caller's email. Idempotent — the web
    calls this on each sign-in, so invites made after signup are picked up too."""
    if not c.email:
        return {"accepted": 0}
    pend = (_service.table("tenant_invitations").select("*")
            .eq("email", c.email).eq("status", "pending").execute().data or [])
    n = 0
    for inv in pend:
        already = (_service.table("tenant_members").select("user_id")
                   .eq("tenant_id", inv["tenant_id"]).eq("user_id", c.user_id)
                   .execute().data)
        if not already:
            _service.table("tenant_members").insert({
                "tenant_id": inv["tenant_id"], "user_id": c.user_id, "role": inv["role"],
            }).execute()
            n += 1
        _service.table("tenant_invitations").update({
            "status": "accepted", "accepted_at": _now_iso(),
        }).eq("invite_id", inv["invite_id"]).execute()
    return {"accepted": n}


@app.post("/api/flows", status_code=201)
def create_flow(body: FlowCreate, c: Caller = Depends(caller)) -> dict:
    tenant_id = _caller_tenant(c, body.tenant_id)   # infer when not given
    _require_editor(c, tenant_id)
    fid = str(uuid.uuid4())
    try:
        c.sb.table("flows").insert({
            "flow_id": fid, "tenant_id": tenant_id, "team": body.team,
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
    d["sf_entry"] = meta.get("sf_entry", False)
    return d


@app.post("/api/flows/{flow_id}/validate")
def validate_flow_ep(flow_id: str, body: FlowIn, c: Caller = Depends(caller)) -> dict:
    meta = _require_visible(c, flow_id)
    errs = _structural_errors(_flow_dict(meta, body))
    return {"valid": not errs, "errors": errs}


@app.post("/api/flows/import/mermaid")
def import_mermaid(body: MermaidIn, c: Caller = Depends(caller)) -> dict:
    """Phase 19a -- parse a Mermaid flowchart into a candidate flow graph.
    Persists nothing: the web editor loads {nodes, edges} as unsaved canvas
    state, and Save/Publish go through the normal validated path."""
    tid = _caller_tenant(c, body.tenant_id)
    _require_editor(c, tid)
    rate_limit(c.user_id, "assist", 30)
    if not (body.text or "").strip():
        raise HTTPException(422, "empty diagram")
    from interpreter.flows.mermaid_import import mermaid_to_flow

    try:
        return mermaid_to_flow(body.text, defaults=NODE_DEFAULTS)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"could not parse the Mermaid diagram: {e}")


@app.post("/api/flows/assist")
def assist_new_flow(body: AssistIn, c: Caller = Depends(caller)) -> dict:
    """Phase 19b -- a plain-English description -> a candidate flow graph.
    Persists nothing; the editor loads it as an unsaved draft."""
    tid = _caller_tenant(c, body.tenant_id)
    _require_editor(c, tid)
    rate_limit(c.user_id, "assist", 12)
    if not (body.prompt or "").strip():
        raise HTTPException(422, "empty prompt")
    from interpreter.flows.assist import assist_generate

    try:
        return assist_generate(body.prompt, defaults=NODE_DEFAULTS)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"assist failed: {e}")


@app.post("/api/flows/{flow_id}/assist")
def assist_edit_flow(flow_id: str, body: AssistEditIn, c: Caller = Depends(caller)) -> dict:
    """Phase 19c -- rewrite the working draft from a plain-English instruction.
    Returns a candidate graph + a diff; persists nothing."""
    meta = _require_visible(c, flow_id)
    _require_editor(c, meta["tenant_id"])
    rate_limit(c.user_id, "assist", 12)
    if not (body.instruction or "").strip():
        raise HTTPException(422, "empty instruction")
    from interpreter.flows.assist import assist_edit

    try:
        current = load_flow(flow_id=flow_id, sb=c.sb, status="draft", validate=False)
    except FlowNotFound:
        raise HTTPException(404, "flow not found")
    try:
        return assist_edit(current, body.instruction, defaults=NODE_DEFAULTS)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"assist failed: {e}")


@app.put("/api/flows/{flow_id}")
def save_flow(flow_id: str, body: FlowIn, c: Caller = Depends(caller)) -> dict:
    """Save the working draft — one transactional RPC. Optimistic concurrency:
    `body.version` must match the flow's current `version` or it's a 409."""
    meta = _require_visible(c, flow_id)
    _require_editor(c, meta["tenant_id"])
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
    _require_editor(c, meta["tenant_id"])
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
    _require_editor(c, meta["tenant_id"])
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
    meta = _require_visible(c, flow_id)
    _require_editor(c, meta["tenant_id"])
    c.sb.table("flows").delete().eq("flow_id", flow_id).execute()  # cascades nodes/edges


class SfEntryIn(BaseModel):
    sf_entry: bool


@app.put("/api/flows/{flow_id}/sf-entry")
def set_sf_entry(flow_id: str, body: SfEntryIn, c: Caller = Depends(caller)) -> dict:
    """Mark (or unmark) this flow as the one `POST /api/hooks/salesforce/case`
    runs. At most one per tenant (migration 042's partial-unique index) — so
    turning it on clears the flag on the tenant's other flows first."""
    meta = _require_visible(c, flow_id)
    _require_editor(c, meta["tenant_id"])
    if body.sf_entry:
        c.sb.table("flows").update({"sf_entry": False}) \
            .eq("tenant_id", meta["tenant_id"]).neq("flow_id", flow_id).execute()
    c.sb.table("flows").update({"sf_entry": body.sf_entry}).eq("flow_id", flow_id).execute()
    return {"sf_entry": body.sf_entry}


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

    # a node like `sf_case` may have mutated the case (sf_id, refreshed tier)
    run_id = record_run(flow, final, case=(final.get("case") or body.case), source="api",
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


class SFCaseHookIn(BaseModel):
    case_id: str
    flow_id: str | None = None       # optional override; else the flow marked `sf_entry`


@app.post("/api/hooks/salesforce/case", status_code=202)
def salesforce_case_hook(
    body: SFCaseHookIn,
    secret: str | None = Header(default=None, alias="X-SF-Hook-Secret"),
) -> dict:
    """Salesforce → automation **push**. A record-triggered Flow on Case
    (After Save, on Create, Status='New', not bot-created) POSTs `{case_id}`
    here; we pull the Case, resolve the flow marked `sf_entry` (the one the
    editor's "Salesforce entry" toggle points at), and queue a `run_flow`
    job (deduped on the Case Id). No user auth — a shared secret
    (`SF_HOOK_SECRET`) gates it. Returns 202."""
    want = os.environ.get("SF_HOOK_SECRET")
    if not want or secret != want:
        raise HTTPException(401, "bad or missing X-SF-Hook-Secret")

    # Enqueue a bare Case Id — the worker hydrates it via `salesforce.get_case`
    # (with retries). Doing the SF read here would call *back* into Salesforce
    # while the triggering @future callout is still blocked on our response,
    # which fails intermittently. Same enqueue path (+ dedupe keys) as the
    # CDC subscriber (`ingestion.sf_cdc_watch`).
    try:
        job_id = sf_ingest.enqueue_case_run(
            _service, body.case_id,
            dedupe_key=f"sfcase:{body.case_id}", idempotency_key=body.case_id,
            trigger="case_created", flow_id=body.flow_id,
        )
    except sf_ingest.EntryFlowError as e:
        raise HTTPException(500, str(e))
    return {"job_id": job_id, "deduped": job_id is None}


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


# ── Phase 22: one timeline per Case (jobs + runs + nodes + errors) ─────
def _q(fn):
    """run a supabase query, swallow any error -> []."""
    try:
        return fn() or []
    except Exception as e:  # noqa: BLE001
        log.warning("trace query failed: %s", e)
        return []


@app.get("/api/trace/{key}")
def get_trace(key: str, format: str = "json", c: Caller = Depends(caller)):
    """Everything that happened for a Case, in order. `key` = a Salesforce
    Case id, a Case number, a run_id, or a job_id. `?format=md` -> plain text.
    Read-only, auth-gated, and **scoped to the caller's tenants** — the service
    client is only used so it can join `jobs` (which have no RLS)."""
    from api.trace import build_timeline, render_markdown

    key = key.strip()
    S = _service
    my_tenants = {
        str(r["tenant_id"]) for r in
        (c.sb.table("tenant_members").select("tenant_id").eq("user_id", c.user_id)
         .execute().data or [])
    }
    if not my_tenants:
        raise HTTPException(403, "not a member of any tenant")

    def _mine(rows: list[dict]) -> list[dict]:
        return [r for r in rows if str(r.get("tenant_id")) in my_tenants]

    runs: dict[str, dict] = {}
    for r in _mine(
            _q(lambda: S.table("runs").select("*").eq("run_id", key).execute().data)
            + _q(lambda: S.table("runs").select("*").eq("case_id", key).execute().data)
            + _q(lambda: S.table("runs").select("*").eq("case_payload->>sf_id", key).execute().data)
            + _q(lambda: S.table("runs").select("*").eq("case_payload->>case_number", key).execute().data)):
        runs[r["run_id"]] = r

    # widen: every case id / number / idempotency key those runs touched
    # ids/ikeys are derived ONLY from the caller's own runs — the widen + jobs
    # lookups below must not reach into another tenant's rows.
    ids: set[str] = set()
    ikeys: set[str] = set()
    for r in runs.values():
        cp = r.get("case_payload") or {}
        for v in (r.get("case_id"), cp.get("sf_id"), cp.get("case_number")):
            if v:
                ids.add(str(v))
        if r.get("idempotency_key"):
            ikeys.add(r["idempotency_key"])
    if runs:
        ids.add(key)          # the raw key is safe once we know it's ours

    # a bare Case number typed straight from Salesforce -> resolve to its Id
    if key.isdigit() and len(key) >= 5:
        try:
            from interpreter import salesforce as _sf
            if _sf.available():
                rec = _sf.client_for(None).query(
                    f"SELECT Id FROM Case WHERE CaseNumber = '{_sf._soql_lit(key)}' LIMIT 1"
                ).get("records", [])
                if rec:
                    ids.add(rec[0]["Id"])
        except Exception as e:  # noqa: BLE001
            log.warning("trace: CaseNumber->Id lookup failed: %s", e)

    for i in list(ids):
        for r in _mine(_q(lambda i=i: S.table("runs").select("*").eq("case_id", i).execute().data)
                       + _q(lambda i=i: S.table("runs").select("*").eq("case_payload->>sf_id", i).execute().data)):
            runs.setdefault(r["run_id"], r)

    jobs: dict[str, dict] = {}
    for jid in list(ids) + ([key] if runs else []):
        for j in (_q(lambda jid=jid: S.table("jobs").select("*").eq("job_id", jid).execute().data)
                  + _q(lambda jid=jid: S.table("jobs").select("*").ilike("dedupe_key", f"%{jid}%").execute().data)
                  + _q(lambda jid=jid: S.table("jobs").select("*").eq("payload->case->>sf_id", jid).execute().data)):
            jobs[j["job_id"]] = j
    for ik in ikeys:
        for j in _q(lambda ik=ik: S.table("jobs").select("*").eq("payload->>idempotency_key", ik).execute().data):
            jobs[j["job_id"]] = j
    # a job belongs to this trace only if it references one of the caller's runs
    # / ids / idempotency keys — never surface a bare cross-tenant job_id match.
    _run_ids = set(runs)
    jobs = {
        jid: j for jid, j in jobs.items()
        if (j.get("payload") or {}).get("run_id") in _run_ids
        or (j.get("payload") or {}).get("idempotency_key") in ikeys
        or any(i and i in str(j.get("dedupe_key") or "") for i in (ids | _run_ids))
        or str(((j.get("payload") or {}).get("case") or {}).get("sf_id") or "") in ids
    }

    if not runs and not jobs:
        raise HTTPException(404, f"nothing found for {key!r} (Case id / number / run_id / job_id)")

    channel_rows = _q(lambda: S.table("tenant_integrations")
                      .select("kind,status,last_error,last_poll_at,tenant_id")
                      .in_("tenant_id", list(my_tenants)).execute().data)

    t = build_timeline(key=key, runs=list(runs.values()), jobs=list(jobs.values()),
                       channel_errors=[r for r in channel_rows if r.get("last_error")])
    if format == "md":
        return PlainTextResponse(render_markdown(t))
    return t


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
    _require_editor(c, tenant_id)
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
    _require_editor(c, col["tenant_id"])
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
    _require_editor(c, _kb_collection(c, sid)["tenant_id"])   # RLS + role gate
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
    _require_editor(c, col["tenant_id"])
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
    _require_editor(c, col["tenant_id"])
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
    _require_editor(c, entry["tenant_id"])
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
    _require_editor(c, col["tenant_id"])
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
    _require_editor(c, col["tenant_id"])
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


# ── Phase 20: email channel ─────────────────────────────────────────
EMAIL_REDIRECT_URI = os.environ.get(
    "EMAIL_GOOGLE_REDIRECT_URI",
    "http://localhost:8000/api/integrations/email/google/callback",
)


class EmailChannelIn(BaseModel):
    provider: str = "imap"                 # 'imap' | 'gmail'
    team: str = "support"
    imap_host: str | None = None
    imap_port: int = 993
    smtp_host: str | None = None
    smtp_port: int = 587
    username: str | None = None
    password: str | None = None            # app password -> Vault; write-only, never returned
    from_addr: str | None = None
    from_name: str | None = None
    no_reply_addr: str | None = None
    folder: str = "INBOX"
    auto_send_enabled: bool = False        # the hard-guard master switch (default off)
    active: bool | None = None             # flip polling on/off without re-entering creds
    tenant_id: str | None = None


def _email_cfg_from_body(tenant_id: str, body: EmailChannelIn, existing):
    from interpreter.mailbox import MailboxConfig

    e = existing or MailboxConfig(tenant_id=tenant_id)
    status = e.status
    if body.active is True:
        status = "active"
    elif body.active is False:
        status = "inactive"
    return MailboxConfig(
        tenant_id=tenant_id,
        provider=body.provider or e.provider,
        team=body.team or e.team,
        username=(body.username or e.username or "").strip(),
        from_addr=(body.from_addr or body.username or e.from_addr or "").strip(),
        from_name=body.from_name if body.from_name is not None else e.from_name,
        no_reply_addr=(body.no_reply_addr or e.no_reply_addr) or None,
        imap_host=(body.imap_host or e.imap_host or "").strip(),
        imap_port=body.imap_port or e.imap_port,
        smtp_host=(body.smtp_host or e.smtp_host or "").strip(),
        smtp_port=body.smtp_port or e.smtp_port,
        folder=body.folder or e.folder,
        auto_send_enabled=bool(body.auto_send_enabled),
        status=status,
        secret=e.secret,
    )


@app.get("/api/integrations/email")
def email_status(tenant_id: str | None = None, c: Caller = Depends(caller)) -> dict:
    """Channel status for the caller's tenant. Never returns the secret."""
    tid = _caller_tenant(c, tenant_id)
    from interpreter.mailbox import gmail_available, load_channel

    base = {"tenant_id": tid, "gmail_available": gmail_available()}
    ch = load_channel(tid, _service)
    if not ch:
        return {**base, "configured": False, "status": "none"}
    row = (_service.table("tenant_integrations")
           .select("last_poll_at,last_error").eq("tenant_id", tid).eq("kind", "email")
           .execute().data or [{}])[0]
    return {**base, **ch.public_status(),
            "last_poll_at": row.get("last_poll_at"), "last_error": row.get("last_error")}


@app.put("/api/integrations/email")
def email_configure(body: EmailChannelIn, c: Caller = Depends(caller)) -> dict:
    tid = _caller_tenant(c, body.tenant_id)
    _require_owner(c, tid)
    rate_limit(c.user_id, "integration", 30)
    from interpreter.mailbox import load_channel, save_channel

    existing = load_channel(tid, _service)
    if body.provider == "imap":
        if not (body.imap_host and body.username):
            raise HTTPException(422, "imap_host and username are required")
        has_pw = bool(body.password) or bool(existing and existing.secret.get("password"))
        if not has_pw:
            raise HTTPException(422, "password (an app password) is required")
    elif body.provider == "gmail":
        if not (existing and existing.secret.get("refresh_token")):
            raise HTTPException(400, "connect Gmail first (Connect Gmail button)")

    cfg = _email_cfg_from_body(tid, body, existing)
    plaintext = None
    if body.provider == "imap" and body.password:
        plaintext = json.dumps({"kind": "imap", "password": body.password})
    save_channel(tid, _service, cfg, plaintext_secret=plaintext, updated_by=c.user_id)
    return email_status(tenant_id=tid, c=c)


@app.delete("/api/integrations/email", status_code=204)
def email_disconnect(tenant_id: str | None = None, c: Caller = Depends(caller)) -> None:
    tid = _caller_tenant(c, tenant_id)
    _require_owner(c, tid)
    from interpreter.mailbox import delete_channel

    delete_channel(tid, _service)


@app.post("/api/integrations/email/test")
def email_test(body: EmailChannelIn, c: Caller = Depends(caller)) -> dict:
    """Log in to the mailbox and back out — saves nothing. Uses the posted
    creds, falling back to the stored secret when the password field is blank."""
    tid = _caller_tenant(c, body.tenant_id)
    _require_owner(c, tid)
    rate_limit(c.user_id, "integration", 20)
    from interpreter.mailbox import load_channel, test_connection

    existing = load_channel(tid, _service)
    cfg = _email_cfg_from_body(tid, body, existing)
    if body.provider == "imap":
        cfg.secret = {"password": body.password
                      or (existing.secret.get("password") if existing else "")}
    elif existing:
        cfg.secret = existing.secret
    return test_connection(cfg)


@app.get("/api/integrations/email/google/authorize")
def email_google_authorize(tenant_id: str | None = None, c: Caller = Depends(caller)) -> dict:
    from interpreter.mailbox import gmail_authorize_url, gmail_available

    if not gmail_available():
        raise HTTPException(503, "Google is not configured on this server")
    tid = _caller_tenant(c, tenant_id)
    _require_owner(c, tid)
    nonce = secrets.token_urlsafe(24)
    _oauth_state[nonce] = (time.time() + 600, c.user_id, tid)
    return {"url": gmail_authorize_url(EMAIL_REDIRECT_URI, nonce)}


@app.get("/api/integrations/email/google/callback")
def email_google_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
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
    _, user_id, tid = hit
    try:
        tok = gdrive.exchange_code(code, EMAIL_REDIRECT_URI)
    except Exception as e:  # noqa: BLE001
        return page(f"Token exchange failed: {e}")

    from interpreter.mailbox import MailboxConfig, gmail_profile_email, load_channel, save_channel

    rt = tok["refresh_token"]
    email_addr = ""
    try:
        email_addr = gmail_profile_email(rt)
    except Exception:  # noqa: BLE001
        pass
    existing = load_channel(tid, _service)
    cfg = existing or MailboxConfig(tenant_id=tid)
    cfg.provider = "gmail"
    if email_addr:
        cfg.username = email_addr
        cfg.from_addr = cfg.from_addr or email_addr
    save_channel(tid, _service, cfg,
                 plaintext_secret=json.dumps({"kind": "gmail", "refresh_token": rt}),
                 updated_by=user_id)
    return page("Gmail connected. You can close this window.")


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
    _require_editor(c, tenant_id)
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
    cur = (c.sb.table("policy_rules").select("rule_id, tenant_id")
           .eq("rule_id", rule_id).execute().data)
    if not cur:
        raise HTTPException(404, "rule not found or not visible to you")
    _require_editor(c, cur[0]["tenant_id"])
    patch = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    patch["updated_by"] = c.user_id
    return c.sb.table("policy_rules").update(patch).eq("rule_id", rule_id).execute().data[0]


@app.delete("/api/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: str, c: Caller = Depends(caller)) -> None:
    rate_limit(c.user_id, "rules_write", 60)
    cur = (c.sb.table("policy_rules").select("tenant_id")
           .eq("rule_id", rule_id).execute().data)
    if cur:
        _require_editor(c, cur[0]["tenant_id"])
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
