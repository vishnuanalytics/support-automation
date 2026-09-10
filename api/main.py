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

from interpreter.builder import build_graph, initial_state  # noqa: E402
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

SUPABASE_URL = os.environ["SUPABASE_URL"].strip()
ANON_KEY = (os.environ.get("SUPABASE_ANON_KEY") or os.environ["SUPABASE_SERVICE_KEY"]).strip()
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"].strip()
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
    "trigger": {"map": {}, "required": [], "defaults": {}},
    "http_request": {"connection": "", "method": "GET", "path": "", "query": {},
                     "out_key": "http", "timeout": 15, "on_error": "passthrough"},
    # FR-47 — any connector (salesforce/slack builtins, or a tenant's own
    # named HTTP connection + saved connection_actions); see GET /api/connectors.
    "connector_action": {"connector": "", "action": "", "params": {},
                         "out_key": "connector_result", "on_error": "passthrough"},
    "transform": {"map": {}, "set": {}, "drop": [], "into": "context"},
    "case_lookup": {"k": 3, "pool": 10, "min_similarity": 0.35},
    # Phase 25 — image attachments, Salesforce context, generic AI prompt
    "attachments": {"source": "salesforce", "max_images": 5, "ocr": True,
                    "skip_signatures": True, "min_image_px": 350,
                    "video": False, "video_frames": 4, "video_max_seconds": 300},
    "sf_context": {"want": ["account", "contacts", "leads", "cases", "team"]},
    # Phase 30 — the filer's recent product activity from the analytics graph
    # (needs a connected PostHog integration + the product_analytics_sync run).
    "product_signal": {"email_field": "contact.email", "out_key": "product_signal"},
    "ai_prompt": {
        "system": "You are a support triage assistant.",
        "user": "Case: {case.subject}\n{case.body}\n\nAccount: {sf_context.account.name} "
                "(tier {sf_context.account.tier})\nImage text: {attachment_text}",
        "model": "openai/gpt-oss-120b",
        "temperature": 0.2,
        "max_tokens": 600,
        "output_key": "ai_output",
        "json_schema": None,
        "images": "none",
        "cache": True,
        "on_error": "passthrough",
    },
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
    # Phase 29 step 2 — a drop-in retrieve+draft pair with a bounded
    # reformulate-and-retry loop, gated on the draft's own groundedness score.
    "agent": {
        "retrieve": {"source": ["supabase", "neo4j"], "top_k": 5},
        "draft": {"model": "openai/gpt-oss-120b", "max_tokens": 900},
        "max_iterations": 3,
        "groundedness_threshold": 0.6,
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
    case: dict[str, Any] = {}
    context: dict[str, Any] = {}   # P5 — generic run payload for a non-Case flow


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


@app.get("/api/templates")
def list_templates_ep(tenant_id: str | None = None, c: Caller = Depends(caller)) -> list[dict]:
    from interpreter import templates
    rows = templates.list_templates()
    # Phase 28 step 5 — best-effort: the built-in gallery must always show
    # even if tenant resolution fails (no workspace yet, or several without
    # an explicit tenant_id) — never let the custom half break the base list.
    try:
        tid = _caller_tenant(c, tenant_id)
        rows = rows + templates.list_custom(c.sb, tid)
    except HTTPException:
        pass
    return rows


@app.get("/api/templates/{template_id}")
def get_template_ep(template_id: str, tenant_id: str | None = None,
                    c: Caller = Depends(caller)) -> dict:
    """P7a — a ready-made flow graph as a candidate the editor loads unsaved
    (same shape as the AI-generate / Mermaid-import paths). Phase 28 step 5:
    falls back to the caller's own saved templates if the id isn't built-in."""
    from interpreter import templates
    g = templates.graph(template_id, defaults=NODE_DEFAULTS)
    if g is None:
        tid = _caller_tenant(c, tenant_id)
        g = templates.custom_graph(c.sb, tid, template_id, defaults=NODE_DEFAULTS)
    if g is None:
        raise HTTPException(404, "unknown template")
    return g


class SaveAsTemplateIn(BaseModel):
    name: str
    category: str = "Custom"
    description: str | None = None


@app.post("/api/flows/{flow_id}/save-as-template", status_code=201)
def save_flow_as_template(flow_id: str, body: SaveAsTemplateIn,
                          c: Caller = Depends(caller)) -> dict:
    """Phase 28 step 5 — snapshot this flow's current draft as a reusable
    template, private to this tenant."""
    from interpreter import audit, templates

    meta = _require_visible(c, flow_id)
    _require_editor(c, meta["tenant_id"])
    if not body.name.strip():
        raise HTTPException(422, "name is required")
    draft = load_flow(flow_id=flow_id, sb=c.sb, status="draft", validate=False)
    row = templates.save_as_template(
        c.sb, meta["tenant_id"], draft["nodes"], draft["edges"],
        name=body.name.strip(), category=body.category.strip() or "Custom",
        description=(body.description or "").strip() or None, created_by=c.user_id,
    )
    audit.record(_service, tenant_id=meta["tenant_id"], action="template.saved",
                actor_id=c.user_id, actor_email=c.email,
                target_type="flow_template", target_id=row.get("template_id"),
                summary=f"saved '{body.name.strip()}' from {meta.get('name') or flow_id}")
    return {"id": row.get("template_id"), "name": row.get("name")}


@app.delete("/api/templates/{template_id}", status_code=204)
def delete_template_ep(template_id: str, tenant_id: str | None = None,
                       c: Caller = Depends(caller)) -> None:
    """Phase 28 step 5 — only a custom (user-saved) template can be deleted;
    the built-in gallery is file-shipped, not a database row."""
    from interpreter import audit, templates

    tid = _caller_tenant(c, tenant_id)
    _require_editor(c, tid)
    deleted = templates.delete_custom(c.sb, tid, template_id)
    if not deleted:
        raise HTTPException(404, "unknown custom template")
    audit.record(_service, tenant_id=tid, action="template.deleted",
                actor_id=c.user_id, actor_email=c.email,
                target_type="flow_template", target_id=template_id,
                summary=f"deleted template {template_id}")


# (tenant_id, org_label) -> (cached_at, data). Was a single global entry
# until 2026-09-03 -- meant every tenant saw whichever org happened to be
# cached first, ignoring the multi-org connector entirely.
_SF_META_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}


@app.get("/api/salesforce/meta")
def salesforce_meta(tenant_id: str | None = None, org: str = "default",
                    c: Caller = Depends(caller)) -> dict:
    """Salesforce routing queues + Case field/picklist data (incl. any
    custom fields), for the flow editor's dropdowns. Tenant+org aware,
    cached 5 min per (tenant, org). `available:false` + empty lists when
    that tenant/org combination can't reach a real Salesforce."""
    import time

    from interpreter import salesforce as _sf

    tid = _caller_tenant(c, tenant_id)
    key = (tid, org or "default")
    now = time.time()
    hit = _SF_META_CACHE.get(key)
    if hit is None or now - hit[0] > 300:
        data = _sf.org_metadata(tid, org)
        _SF_META_CACHE[key] = (now, data)
    return _SF_META_CACHE[key][1]


# tenant_id -> (cached_at, data). Same pattern as _SF_META_CACHE.
_SLACK_META_CACHE: dict[str, tuple[float, dict]] = {}


@app.get("/api/slack/meta")
def slack_meta(tenant_id: str | None = None, c: Caller = Depends(caller)) -> dict:
    """Slack channels + users + usergroups for the flow editor's pickers
    (`notify_human`). Tenant-scoped, cached 5 min. `available:false` +
    empty lists when the tenant hasn't connected Slack."""
    import time

    from interpreter import slack as _slack

    tid = _caller_tenant(c, tenant_id)   # membership already verified via c.sb (RLS)
    now = time.time()
    hit = _SLACK_META_CACHE.get(tid)
    if hit is None or now - hit[0] > 300:
        # tenant_integrations has RLS enabled with NO policy (service-role
        # only, by design -- it holds secrets). Passing c.sb here silently
        # returned zero rows for every tenant, so workspace_meta always saw
        # "not connected" regardless of a real connection. Let it fall back
        # to its own service-role client, same as salesforce_meta already
        # does for org_metadata -- the tenant scoping is enforced above by
        # _caller_tenant, not by RLS on this table.
        data = _slack.workspace_meta(tid)
        _SLACK_META_CACHE[tid] = (now, data)
    return _SLACK_META_CACHE[tid][1]


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
    rows = (c.sb.table("tenant_members").select("tenant_id, role")
            .eq("user_id", c.user_id).execute().data or [])
    names = {t["tenant_id"]: t["name"] for t in
             (c.sb.table("tenants").select("tenant_id, name").execute().data or [])}
    return [{**r, "name": names.get(r["tenant_id"])} for r in rows]


class TenantIn(BaseModel):
    name: str


@app.post("/api/tenants", status_code=201)
def create_tenant(body: TenantIn, c: Caller = Depends(caller)) -> dict:
    """P7d — self-serve: create a named workspace with the caller as owner."""
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "name is required")
    tid = str(uuid.uuid4())
    _service.table("tenants").insert(
        {"tenant_id": tid, "name": name, "created_by": c.user_id}).execute()
    _service.table("tenant_members").insert(
        {"tenant_id": tid, "user_id": c.user_id, "role": "owner"}).execute()

    from interpreter import audit
    audit.record(_service, tenant_id=tid, action="tenant.created",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="tenant", target_id=tid, summary=f"created workspace {name!r}")
    return {"tenant_id": tid, "name": name, "role": "owner"}


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

    from interpreter import audit
    removed_email = _emails_for([user_id]).get(user_id, "")
    audit.record(_service, tenant_id=tid, action="member.removed",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="member", target_id=user_id,
                 summary=f"removed {removed_email or user_id}")


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
        row = c.sb.table("tenant_invitations").insert({
            "tenant_id": tid, "email": email, "role": role, "invited_by": c.user_id,
        }).execute().data[0]
    except Exception as e:  # noqa: BLE001  — dup pending invite, etc.
        raise HTTPException(409, f"could not invite {email}: {e}")

    from interpreter import audit
    audit.record(_service, tenant_id=tid, action="invitation.created",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="invitation", target_id=row["invite_id"],
                 summary=f"invited {email} as {role}")
    return row


@app.delete("/api/invitations/{invite_id}", status_code=204)
def revoke_invitation(invite_id: str, c: Caller = Depends(caller)) -> None:
    cur = (c.sb.table("tenant_invitations").select("tenant_id, email")
           .eq("invite_id", invite_id).execute().data)
    if cur:
        _require_owner(c, cur[0]["tenant_id"])
    c.sb.table("tenant_invitations").update({"status": "revoked"}) \
        .eq("invite_id", invite_id).execute()

    if cur:
        from interpreter import audit
        audit.record(_service, tenant_id=cur[0]["tenant_id"], action="invitation.revoked",
                     actor_id=c.user_id, actor_email=c.email,
                     target_type="invitation", target_id=invite_id,
                     summary=f"revoked invite to {cur[0].get('email', '?')}")


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
            from interpreter import audit
            audit.record(_service, tenant_id=inv["tenant_id"], action="invitation.accepted",
                         actor_id=c.user_id, actor_email=c.email,
                         target_type="member", target_id=c.user_id,
                         summary=f"{c.email or c.user_id} joined as {inv['role']}")
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

    from interpreter import audit
    audit.record(_service, tenant_id=tenant_id, action="flow.created",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="flow", target_id=fid,
                 summary=f"created flow {body.name!r} ({body.team})")
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

    from interpreter import audit
    audit.record(_service, tenant_id=meta["tenant_id"], action="flow.published",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="flow", target_id=flow_id,
                 summary=f"published {meta.get('name') or flow_id} v{version}",
                 metadata={"version": version})
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

    from interpreter import audit
    audit.record(_service, tenant_id=meta["tenant_id"], action="flow.rolled_back",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="flow", target_id=flow_id,
                 summary=(f"rolled back {meta.get('name') or flow_id} "
                          f"from v{meta.get('published_version')} to v{body.version}"),
                 metadata={"from_version": meta.get("published_version"),
                           "to_version": body.version})
    return {"published_version": body.version}


@app.delete("/api/flows/{flow_id}", status_code=204)
def delete_flow(flow_id: str, c: Caller = Depends(caller)) -> None:
    meta = _require_visible(c, flow_id)
    _require_editor(c, meta["tenant_id"])
    c.sb.table("flows").delete().eq("flow_id", flow_id).execute()  # cascades nodes/edges

    from interpreter import audit
    audit.record(_service, tenant_id=meta["tenant_id"], action="flow.deleted",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="flow", target_id=flow_id,
                 summary=f"deleted {meta.get('name') or flow_id}")


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

    from interpreter import audit
    audit.record(_service, tenant_id=meta["tenant_id"],
                 action="flow.sf_entry_set" if body.sf_entry else "flow.sf_entry_unset",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="flow", target_id=flow_id,
                 summary=f"{'marked' if body.sf_entry else 'unmarked'} as the Salesforce entry flow")
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
        final = build_graph(flow).invoke(initial_state(flow, case=body.case, context=body.context))
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


@app.post("/api/triggers/{flow_id}", status_code=202)
def trigger_run(flow_id: str, body: dict[str, Any],
                idempotency_key: str | None = None,
                c: Caller = Depends(caller)) -> dict:
    """P5b — start a flow from a generic payload (a `trigger` flow, no Case).
    The JSON body becomes `state.context`; the flow reads it as `context.*` /
    `input.*`. Async — returns a job id."""
    from interpreter import triggers
    rate_limit(c.user_id, "enqueue", 120)
    _require_visible(c, flow_id)
    ctx = triggers.webhook_context(body, source="webhook")["context"]
    job_id = jobs.enqueue(
        "run_flow",
        {"flow_id": flow_id, "context": ctx, "idempotency_key": idempotency_key},
        dedupe_key=idempotency_key, sb=_service,
    )
    if job_id is None:
        return {"job_id": None, "deduped": True}
    return {"job_id": job_id}


# ── P6a: webhook / schedule triggers for a flow ──────────────────────
class TriggerIn(BaseModel):
    kind: str = "webhook"           # 'webhook' | 'schedule'
    cron: str | None = None         # schedule only
    label: str | None = None


def _public_base() -> str:
    return os.environ.get("PUBLIC_API_BASE", "").rstrip("/") or "http://localhost:8000"


def _trigger_view(t: dict) -> dict:
    out = {k: t.get(k) for k in ("trigger_id", "kind", "cron", "label", "enabled",
                                 "last_fired_at", "fire_count", "created_at")}
    if t.get("kind") == "webhook" and t.get("token"):
        out["url"] = f"{_public_base()}/t/{t['token']}"
    return out


@app.get("/api/flows/{flow_id}/triggers")
def list_triggers(flow_id: str, c: Caller = Depends(caller)) -> list[dict]:
    _require_visible(c, flow_id)
    rows = (c.sb.table("flow_triggers").select("*")
            .eq("flow_id", flow_id).order("created_at").execute().data or [])
    return [_trigger_view(t) for t in rows]


@app.post("/api/flows/{flow_id}/triggers", status_code=201)
def create_trigger(flow_id: str, body: TriggerIn, c: Caller = Depends(caller)) -> dict:
    flow = _require_visible(c, flow_id)
    _require_editor(c, flow["tenant_id"])
    if body.kind not in ("webhook", "schedule"):
        raise HTTPException(422, "kind must be webhook | schedule")
    if body.kind == "schedule" and not body.cron:
        raise HTTPException(422, "a schedule trigger needs a cron expression")
    row = {"flow_id": flow_id, "tenant_id": flow["tenant_id"], "kind": body.kind,
           "cron": body.cron, "label": body.label, "created_by": c.user_id}
    if body.kind == "webhook":
        row["token"] = secrets.token_urlsafe(24)
    created = _service.table("flow_triggers").insert(row).execute().data[0]

    from interpreter import audit
    audit.record(_service, tenant_id=flow["tenant_id"], action="trigger.created",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="trigger", target_id=created["trigger_id"],
                 summary=f"added a {body.kind} trigger" + (f" ({body.label})" if body.label else ""))
    return _trigger_view(created)


@app.delete("/api/flows/{flow_id}/triggers/{trigger_id}", status_code=204)
def delete_trigger(flow_id: str, trigger_id: str, c: Caller = Depends(caller)) -> None:
    flow = _require_visible(c, flow_id)
    _require_editor(c, flow["tenant_id"])
    _service.table("flow_triggers").delete().eq("trigger_id", trigger_id) \
        .eq("flow_id", flow_id).execute()

    from interpreter import audit
    audit.record(_service, tenant_id=flow["tenant_id"], action="trigger.deleted",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="trigger", target_id=trigger_id, summary="removed a trigger")


@app.post("/t/{token}", status_code=202)
def fire_webhook(token: str, body: dict[str, Any],
                 idempotency_key: str | None = None) -> dict:
    """P6a — the public webhook. No auth: the token IS the credential. The JSON
    body becomes `state.context`; the flow reads `context.*` / `input.*`."""
    from interpreter import triggers
    rate_limit(token, "webhook", 300)
    try:
        rows = (_service.table("flow_triggers").select("*")
                .eq("token", token).eq("kind", "webhook").eq("enabled", True)
                .limit(1).execute().data or [])
    except Exception:  # noqa: BLE001 — can't verify the token -> reject
        rows = []
    if not rows:
        raise HTTPException(404, "unknown or disabled trigger")
    trg = rows[0]
    ctx = triggers.webhook_context(body, source="webhook")["context"]
    job_id = jobs.enqueue(
        "run_flow",
        {"flow_id": trg["flow_id"], "context": ctx, "idempotency_key": idempotency_key},
        dedupe_key=idempotency_key, sb=_service)
    try:
        _service.table("flow_triggers").update({
            "last_fired_at": _now_iso(), "fire_count": (trg.get("fire_count") or 0) + 1,
        }).eq("trigger_id", trg["trigger_id"]).execute()
    except Exception:  # noqa: BLE001
        pass
    return {"job_id": job_id, "deduped": job_id is None}


# ── Multi-provider connectors, step 3: the Freshchat channel ─────────────
@app.post("/webhooks/freshchat/{tenant_id}", status_code=202)
async def freshchat_webhook(tenant_id: str, request: Request) -> dict:
    """Public webhook (no bearer auth — the tenant_id in the URL plus a
    valid `X-Freshchat-Signature` from THAT tenant's own stored webhook
    public key are the credential, verified before the body is trusted at
    all). Turns a customer `message_create` event into a case-shaped
    `run_flow` job, reusing the same case across a conversation via
    `channel_threads` (migration 085) — the same "produce a case dict,
    enqueue run_flow" shape the email channel uses, not the generic
    Case-less webhook-trigger machinery (`/t/{token}`), since this needs to
    flow through the real case-touching pipeline (`identify`/`sf_case`/...).
    """
    from interpreter import channel_threads, freshchat

    raw = await request.body()
    rate_limit(tenant_id, "freshchat_webhook", 300)

    try:
        cfg = freshchat.load_channel(tenant_id, _service)
    except Exception:  # noqa: BLE001 — can't verify the tenant -> reject (matches /t/{token})
        cfg = None
    if not cfg or not cfg.webhook_public_key:
        raise HTTPException(404, "freshchat not connected for this tenant")
    if not freshchat.verify_signature(
        cfg.webhook_public_key, raw, request.headers.get("X-Freshchat-Signature"),
    ):
        raise HTTPException(401, "bad freshchat signature")

    import json as _json
    try:
        body = _json.loads(raw.decode())
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "invalid JSON")

    parsed = freshchat.parse_webhook_message(body)
    if not parsed:
        return {"ok": True, "skipped": "not a new customer message"}

    conv_id = parsed["conversation_id"]
    existing = channel_threads.get_case_ref(tenant_id, freshchat.KIND, conv_id, sb=_service)
    case: dict[str, Any] = {
        "tenant_id": tenant_id, "team": cfg.team, "channel": freshchat.KIND,
        "conversation_id": conv_id, "case_id": f"freshchat:{conv_id}",
        "subject": parsed["text"][:120], "body": parsed["text"],
        "from": parsed.get("actor_id") or "",
    }
    if existing:
        case["sf_id"] = existing["case_ref"]
        case["case_number"] = existing.get("case_number")

    try:
        flow = load_flow(tenant_id=tenant_id, team=cfg.team, status="published", sb=_service)
    except (FlowNotFound, FlowInvalid):
        raise HTTPException(404, f"no published '{cfg.team}' flow for this tenant")

    # Python's builtin hash() is randomized per-process (PYTHONHASHSEED) --
    # useless as a dedupe key across a worker restart. A stable digest is
    # the fallback when Freshchat's payload doesn't carry its own message id
    # (see freshchat.parse_webhook_message's docstring on that uncertainty).
    fallback_id = hashlib.sha256(f"{conv_id}:{parsed['text']}".encode()).hexdigest()[:16]
    idem = f"freshchat:{parsed.get('message_id') or fallback_id}"
    job_id = jobs.enqueue(
        "run_flow", {"flow_id": flow["flow_id"], "case": case, "idempotency_key": idem},
        dedupe_key=idem, sb=_service,
    )
    return {"job_id": job_id, "deduped": job_id is None}


# ── P6c: per-tenant HTTP connections for the `http_request` node ─────
class ConnectionIn(BaseModel):
    slug: str
    base_url: str
    auth: dict[str, Any] = {}        # {type, header_name, token/value/username/password}
    tenant_id: str | None = None


@app.get("/api/connections")
def list_connections(tenant_id: str | None = None, c: Caller = Depends(caller)) -> list[dict]:
    from interpreter import connections
    tid = _caller_tenant(c, tenant_id)
    rows = (_service.table("connections").select("*")
            .eq("tenant_id", tid).order("slug").execute().data or [])
    return [connections.redact(r) for r in rows]       # never the secret


@app.post("/api/connections", status_code=201)
def create_connection(body: ConnectionIn, c: Caller = Depends(caller)) -> dict:
    from interpreter import connections
    tid = _caller_tenant(c, body.tenant_id)
    _require_editor(c, tid)
    from interpreter.net_safety import is_public_http_url
    ok, reason = is_public_http_url(body.base_url)
    if not ok:
        raise HTTPException(422, f"base_url rejected: {reason}")
    row = (_service.table("connections").upsert({
        "tenant_id": tid, "slug": body.slug.strip(), "base_url": body.base_url.rstrip("/"),
        "auth": body.auth, "created_by": c.user_id, "updated_at": _now_iso(),
    }, on_conflict="tenant_id,slug").execute().data[0])

    from interpreter import audit
    audit.record(_service, tenant_id=tid, action="connection.added",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="connection", target_id=body.slug.strip(),
                 summary=f"added connection {body.slug.strip()} -> {body.base_url.rstrip('/')}")
    return connections.redact(row)


@app.delete("/api/connections/{slug}", status_code=204)
def delete_connection(slug: str, tenant_id: str | None = None,
                      c: Caller = Depends(caller)) -> None:
    tid = _caller_tenant(c, tenant_id)
    _require_editor(c, tid)
    _service.table("connections").delete().eq("tenant_id", tid).eq("slug", slug).execute()

    from interpreter import audit
    audit.record(_service, tenant_id=tid, action="connection.removed",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="connection", target_id=slug,
                 summary=f"removed connection {slug}")


class ConnectionActionIn(BaseModel):
    name: str
    method: str = "GET"
    path: str
    params: list[dict[str, Any]] = []
    body_template: Any = None


@app.get("/api/connections/{slug}/actions")
def list_connection_actions(slug: str, tenant_id: str | None = None,
                            c: Caller = Depends(caller)) -> list[dict]:
    from interpreter import connections
    tid = _caller_tenant(c, tenant_id)
    conn = connections.resolve(tid, slug, sb=_service)
    if not conn:
        raise HTTPException(404, "connection not found")
    return connections.list_actions(conn["connection_id"], sb=_service)


@app.post("/api/connections/{slug}/actions", status_code=201)
def save_connection_action(slug: str, body: ConnectionActionIn, tenant_id: str | None = None,
                           c: Caller = Depends(caller)) -> dict:
    from interpreter import connections
    tid = _caller_tenant(c, tenant_id)
    _require_editor(c, tid)
    conn = connections.resolve(tid, slug, sb=_service)
    if not conn:
        raise HTTPException(404, "connection not found")
    row = connections.save_action(conn["connection_id"], body.name, method=body.method,
                                  path=body.path, params=body.params,
                                  body_template=body.body_template, sb=_service)

    from interpreter import audit
    audit.record(_service, tenant_id=tid, action="connection_action.added",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="connection", target_id=f"{slug}/{body.name}",
                 summary=f"saved action {body.name} on connection {slug}")
    return row


@app.delete("/api/connections/{slug}/actions/{name}", status_code=204)
def delete_connection_action(slug: str, name: str, tenant_id: str | None = None,
                             c: Caller = Depends(caller)) -> None:
    from interpreter import connections
    tid = _caller_tenant(c, tenant_id)
    _require_editor(c, tid)
    conn = connections.resolve(tid, slug, sb=_service)
    if not conn:
        raise HTTPException(404, "connection not found")
    connections.delete_action(conn["connection_id"], name, sb=_service)

    from interpreter import audit
    audit.record(_service, tenant_id=tid, action="connection_action.removed",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="connection", target_id=f"{slug}/{name}",
                 summary=f"removed action {name} on connection {slug}")


@app.get("/api/connectors")
def list_connectors_ep(tenant_id: str | None = None, c: Caller = Depends(caller)) -> list[dict]:
    """FR-47 — the full connector catalog (the salesforce/slack builtins plus
    this tenant's own HTTP connections-as-connectors) for the flow editor's
    connector/action pickers. `impl` is a Python callable, never serialized."""
    from interpreter import connectors
    tid = _caller_tenant(c, tenant_id)
    specs = connectors.list_connectors(tid, sb=_service)
    return [
        {"slug": s.slug, "label": s.label, "auth": s.auth,
         "actions": [{"name": a.name, "description": a.description, "params": a.params}
                     for a in s.actions.values()]}
        for s in specs
    ]


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
@app.get("/api/audit")
def list_audit(tenant_id: str | None = None, action: str | None = None,
               limit: int = 100, c: Caller = Depends(caller)) -> list[dict]:
    """Phase 28 — the platform activity log. Member-readable, like Runs."""
    tid = _caller_tenant(c, tenant_id)
    q = (c.sb.table("audit_log").select("*").eq("tenant_id", tid)
         .order("created_at", desc=True).limit(min(max(limit, 1), 300)))
    if action:
        q = q.eq("action", action)
    return q.execute().data or []


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


# ── P9: usage & billing dashboard ───────────────────────────────────────
@app.get("/api/billing/usage")
def billing_usage(tenant_id: str | None = None, period: str | None = None,
                  c: Caller = Depends(caller)) -> dict:
    """Runs + tokens + a notional cost estimate for one calendar month,
    against the tenant's static plan quota. Owner-only — same bar as
    /api/members. No payment processing behind this; see interpreter/billing.py."""
    from interpreter import billing

    tid = _caller_tenant(c, tenant_id)
    _require_owner(c, tid)
    try:
        period_label, period_start, period_end = billing.month_bounds(period)
    except (ValueError, TypeError):
        raise HTTPException(422, "period must be YYYY-MM")

    trows = c.sb.table("tenants").select("plan").eq("tenant_id", tid).execute().data or []
    plan = (trows[0].get("plan") if trows else None) or "free"

    rows = (
        c.sb.table("runs")
        .select("flow_id, tokens_total, tokens_by_model, tokens_by_node, created_at")
        .eq("tenant_id", tid)
        .gte("created_at", period_start).lt("created_at", period_end)
        .limit(5000).execute().data
        or []
    )
    flow_names = {
        f["flow_id"]: f["name"]
        for f in (c.sb.table("flows").select("flow_id, name")
                  .eq("tenant_id", tid).execute().data or [])
    }
    return {"period_label": period_label,
            **billing.usage_summary(rows, plan, period_start, period_end, flow_names)}


@app.get("/api/billing/flow-deltas")
def billing_flow_deltas(tenant_id: str | None = None, c: Caller = Depends(caller)) -> list[dict]:
    """Which flows got more/less expensive per run right after their last
    published edit — mean tokens/run in the 14 days before the newest
    `flow_versions.created_at` vs since. Only flows with >= 5 runs on each
    side are reported; `ratio` > 1 = pricier now. Owner-only."""
    from datetime import datetime, timedelta, timezone

    tid = _caller_tenant(c, tenant_id)
    _require_owner(c, tid)
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(days=45)).isoformat()

    flows = {f["flow_id"]: f["name"] for f in
             (c.sb.table("flows").select("flow_id, name").eq("tenant_id", tid).execute().data or [])}
    if not flows:
        return []
    # newest version per flow
    latest: dict[str, str] = {}
    for v in (c.sb.table("flow_versions").select("flow_id, created_at")
              .in_("flow_id", list(flows)).order("created_at", desc=True)
              .limit(2000).execute().data or []):
        latest.setdefault(v["flow_id"], v["created_at"])

    runs = (c.sb.table("runs").select("flow_id, tokens_total, created_at")
            .eq("tenant_id", tid).gte("created_at", window_start)
            .limit(8000).execute().data or [])

    agg: dict[str, dict[str, list[int]]] = {}
    for r in runs:
        fid, edited = r.get("flow_id"), latest.get(r.get("flow_id"))
        if not (fid and edited):
            continue
        side = "after" if r["created_at"] >= edited else "before"
        agg.setdefault(fid, {"before": [], "after": []})[side].append(int(r.get("tokens_total") or 0))

    out = []
    for fid, s in agg.items():
        if len(s["before"]) < 5 or len(s["after"]) < 5:
            continue
        b = sum(s["before"]) / len(s["before"])
        a = sum(s["after"]) / len(s["after"])
        out.append({
            "flow_id": fid, "name": flows.get(fid, fid),
            "edited_at": latest[fid],
            "before_avg_tokens": round(b), "after_avg_tokens": round(a),
            "ratio": round(a / b, 2) if b else None,
            "runs_before": len(s["before"]), "runs_after": len(s["after"]),
        })
    out.sort(key=lambda x: -(x["ratio"] or 0))
    return out


# ── KIL-f: the Knowledge Integrity Loop review queue + metrics ─────────
class ReviewResolveIn(BaseModel):
    status: str  # 'correct' | 'wrong' | 'dismissed'


@app.get("/api/review-tasks")
def list_review_tasks(status: str | None = "open", limit: int = 100,
                      c: Caller = Depends(caller)) -> list[dict]:
    q = (c.sb.table("review_tasks")
         .select("id, case_sf_id, case_number, run_id, kind, trigger, statement, "
                 "verdict, contexts, status, reviewer_id, reviewed_at, kb_change_id, "
                 "slack_channel, slack_ts, created_at")
         .order("created_at", desc=True).limit(min(max(limit, 1), 500)))
    if status and status != "all":
        q = q.eq("status", status)
    return q.execute().data or []


@app.post("/api/review-tasks/{task_id}/resolve")
def resolve_review_task(task_id: str, body: ReviewResolveIn,
                        c: Caller = Depends(caller)) -> dict:
    from interpreter import approvals
    if body.status not in ("correct", "wrong", "dismissed"):
        raise HTTPException(422, "status must be correct | wrong | dismissed")
    # RLS-checked read first: the caller must be able to see the task
    seen = (c.sb.table("review_tasks").select("id, tenant_id")
            .eq("id", task_id).execute().data or [])
    if not seen:
        raise HTTPException(404, "review task not found")
    rate_limit(c.user_id, "review", 60)
    res = approvals.resolve_review_task(
        _service, task_id, status=body.status, reviewed_by=c.user_id)
    if res.get("skipped"):
        raise HTTPException(409, "task already resolved")
    return res


@app.get("/api/kil/metrics")
def kil_metrics_ep(days: int = 30, tenant_id: str | None = None,
                   c: Caller = Depends(caller)) -> dict:
    from interpreter import kil_metrics
    tid = _caller_tenant(c, tenant_id)
    return kil_metrics.compute(c.sb, tid, days=min(max(days, 1), 180))


@app.get("/api/health/tenant")
def tenant_health(tenant_id: str | None = None, c: Caller = Depends(caller)) -> dict:
    """One 'is my bot healthy' payload for a tenant — the signals that are
    already tenant-scoped: connected sources failing to sync, the KIL review
    backlog, reasoning sessions stuck open, doc write-backs still awaiting a
    human, and the last-24h run-outcome mix (a high handover / need-info rate
    means the bot is struggling to answer). Read-only, cheap counts."""
    from datetime import datetime, timedelta, timezone

    tid = _caller_tenant(c, tenant_id)
    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(hours=24)).isoformat()
    stuck_before = (now - timedelta(hours=6)).isoformat()

    conns = (c.sb.table("kb_source_connections")
             .select("status, last_result")
             .eq("tenant_id", tid).neq("status", "archived").execute().data or [])
    failing = [x for x in conns if x.get("status") == "error"]

    open_tasks = (c.sb.table("review_tasks").select("created_at")
                  .eq("tenant_id", tid).eq("status", "open").execute().data or [])
    oldest_days = None
    if open_tasks:
        oldest = min(t["created_at"] for t in open_tasks)
        oldest_days = round((now - datetime.fromisoformat(oldest.replace("Z", "+00:00"))).days, 1)

    reasoning_stuck = len(
        c.sb.table("reasoning_sessions").select("session_id")
        .eq("tenant_id", tid).in_("state", ["open", "clarifying", "reasoning"])
        .lt("updated_at", stuck_before).execute().data or [])

    wb_pending = len(
        c.sb.table("kb_doc_writebacks").select("id")
        .eq("tenant_id", tid).in_("status", ["applied", "partial"]).execute().data or [])

    failed_jobs = len(
        _service.table("jobs").select("job_id")
        .eq("tenant_id", tid).eq("status", "failed")
        .gte("updated_at", day_ago).execute().data or [])

    runs = (c.sb.table("runs").select("outcome")
            .eq("tenant_id", tid).gte("created_at", day_ago).limit(2000).execute().data or [])
    by_outcome: dict[str, int] = {}
    for r in runs:
        by_outcome[r.get("outcome") or "?"] = by_outcome.get(r.get("outcome") or "?", 0) + 1
    n = len(runs) or 1
    struggle = by_outcome.get("handover", 0) + by_outcome.get("need_info", 0)

    sys_stale = []
    for h in (_service.table("system_health").select("component, last_healthy_at")
              .execute().data or []):
        lh = h.get("last_healthy_at")
        if lh:
            hrs = (now - datetime.fromisoformat(lh.replace("Z", "+00:00"))).total_seconds() / 3600
            if hrs > 26:
                sys_stale.append({"component": h["component"], "stale_hours": round(hrs, 1)})

    return {
        "tenant_id": tid,
        "connections": {"failing": len(failing),
                        "sample": ((failing[0].get("last_result") or {}).get("error")
                                   if failing else None)},
        "review_backlog": {"open": len(open_tasks), "oldest_days": oldest_days},
        "reasoning_stuck": reasoning_stuck,
        "doc_writebacks_pending": wb_pending,
        "failed_jobs_24h": failed_jobs,
        "runs_24h": {"total": len(runs), "by_outcome": by_outcome,
                     "struggle_rate": round(struggle / n, 3)},
        "system_stale": sys_stale,
    }


@app.get("/api/jobs/failures")
def job_failures(hours: int = 24, limit: int = 50, tenant_id: str | None = None,
                 c: Caller = Depends(caller)) -> dict:
    """Jobs that ran out of retries for this tenant in the last `hours`.
    Owner-only. Never returns the payload (it can hold case text) — only
    the kind, attempt count, truncated error and timestamps."""
    from datetime import datetime, timedelta, timezone

    tid = _caller_tenant(c, tenant_id)
    _require_owner(c, tid)
    hours = min(max(hours, 1), 168)
    limit = min(max(limit, 1), 200)
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    rows = (_service.table("jobs")
            .select("job_id, kind, attempts, max_attempts, error, dedupe_key, "
                    "created_at, updated_at")
            .eq("tenant_id", tid).eq("status", "failed")
            .gte("updated_at", since)
            .order("updated_at", desc=True).limit(limit).execute().data or [])
    by_kind: dict[str, int] = {}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    return {"window_hours": hours, "total": len(rows),
            "by_kind": by_kind, "failures": rows}


class GraphAskIn(BaseModel):
    question: str
    tenant_id: str | None = None


@app.post("/api/graph/ask")
def graph_ask(body: GraphAskIn, c: Caller = Depends(caller)) -> dict:
    """Owner-only 'ask the case graph in English'. The question is turned
    into a bounded JSON query spec by an LLM, then compiled deterministically
    to a read-only, tenant-scoped Cypher query — the LLM never authors
    Cypher (see interpreter/graph_query.py). Returns the compiled query
    alongside the rows for transparency."""
    from interpreter import graph_query

    tid = _caller_tenant(c, body.tenant_id)
    _require_owner(c, tid)
    try:
        return graph_query.answer(body.question, tid)
    except graph_query.GraphQueryError as e:
        raise HTTPException(422, str(e))


@app.get("/api/graph/related")
def graph_related_ep(case: str, tenant_id: str | None = None,
                     c: Caller = Depends(caller)) -> dict:
    """Owner-only. Cases linked to `?case=<number>` by a shared Issue (same
    extracted root cause) or a DUPLICATE_OF edge — "the same underlying bug
    as this one". Populate Issues with `python -m interpreter.case_cluster`."""
    from interpreter import graph_query

    tid = _caller_tenant(c, tenant_id)
    _require_owner(c, tid)
    try:
        return graph_query.related_cases(case, tid)
    except graph_query.GraphQueryError as e:
        raise HTTPException(422, str(e))


@app.post("/api/ask")
def ask_ep(body: GraphAskIn, c: Caller = Depends(caller)) -> dict:
    """Owner-only 'ask in English' with a router: the question goes to the
    case graph first (see `/api/graph/ask`); when it isn't expressible as a
    graph query — or the graph is empty and the question reads like prose —
    it falls through to RAG over similar resolved cases. The response
    carries `mode: "graph" | "rag"`."""
    from interpreter import graph_query

    tid = _caller_tenant(c, body.tenant_id)
    _require_owner(c, tid)
    try:
        return graph_query.ask(body.question, tid, sb=c.sb)
    except graph_query.GraphQueryError as e:
        raise HTTPException(422, str(e))


@app.get("/api/kil/digest")
def kil_digest_ep(weeks: int = 4, tenant_id: str | None = None,
                  format: str = "json", c: Caller = Depends(caller)) -> Any:
    """P8a — the weekly learning report (this week vs last, recurring
    contradictions, KB changes). `?format=md` for the Slack-flavoured text."""
    from interpreter import kil_metrics
    tid = _caller_tenant(c, tenant_id)
    d = kil_metrics.digest(c.sb, tid, weeks=min(max(weeks, 1), 12))
    if format == "md":
        return PlainTextResponse(kil_metrics.render_digest(d))
    return {**d, "markdown": kil_metrics.render_digest(d)}


# ── P4 (FR-44): one approvals inbox — review tasks + action requests ──
class ActionDecisionIn(BaseModel):
    decision: str  # 'approve' | 'reject'


@app.get("/api/approvals")
def list_approvals(c: Caller = Depends(caller)) -> dict:
    """Everything waiting on a human, in one place — so a manager never has to
    be in Slack to clear it. Both reads are RLS-scoped to the caller."""
    tasks = (c.sb.table("review_tasks")
             .select("id, case_sf_id, case_number, run_id, kind, trigger, statement, "
                     "verdict, contexts, status, kb_change_id, created_at")
             .eq("status", "open").order("created_at", desc=True).limit(200)
             .execute().data or [])
    ars = (c.sb.table("action_requests")
           .select("id, run_id, rule_name, kind, payload, status, slack_channel, "
                   "slack_ts, created_at")
           .eq("status", "pending").order("created_at", desc=True).limit(200)
           .execute().data or [])
    return {"review_tasks": tasks, "action_requests": ars}


@app.post("/api/approvals/action-requests/{ar_id}")
def decide_action_request_ep(ar_id: str, body: ActionDecisionIn,
                             c: Caller = Depends(caller)) -> dict:
    from interpreter import approvals
    if body.decision not in ("approve", "reject"):
        raise HTTPException(422, "decision must be approve | reject")
    seen = (c.sb.table("action_requests").select("id")
            .eq("id", ar_id).execute().data or [])
    if not seen:
        raise HTTPException(404, "action request not found")
    rate_limit(c.user_id, "review", 60)
    res = approvals.decide_action_request(
        _service, ar_id, approve=(body.decision == "approve"), decided_by=c.user_id)
    if res.get("skipped"):
        raise HTTPException(409, f"already {res['skipped']}")
    sl, ar = res["slack"], res["ar"]
    try:
        if sl["channel"] and sl["ts"] and slackmod.available():
            slackmod.update_message(ar["tenant_id"], sl["channel"], sl["ts"], sl["text"], _service)
    except Exception:  # noqa: BLE001
        pass

    from interpreter import audit
    title = (ar.get("payload") or {}).get("title") or ar.get("kind") or "request"
    audit.record(_service, tenant_id=ar["tenant_id"], action=f"approval.{res['status']}",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type=ar.get("kind") or "action_request", target_id=ar_id,
                 summary=f"{res['status']} {title}")
    return {"status": res["status"], "job_kind": res["job_kind"]}


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

    # Phase 27 — the Status / routing / breach spine. `sf_ids` come only from
    # the caller's own runs, so rows keyed on them are already tenant-scoped
    # (sweep-written events may carry a null tenant_id — don't drop those).
    sf_ids = [i for i in ids if str(i).startswith("500")]
    case_events: list[dict] = []
    if sf_ids:
        case_events = _q(lambda: S.table("case_events").select("*")
                         .in_("case_sf_id", sf_ids).order("ts").execute().data)

    t = build_timeline(key=key, runs=list(runs.values()), jobs=list(jobs.values()),
                       channel_errors=[r for r in channel_rows if r.get("last_error")],
                       case_events=case_events)
    if format == "md":
        return PlainTextResponse(render_markdown(t))
    return t


@app.post("/api/trace/{key}/retry")
def retry_trace(key: str, c: Caller = Depends(caller)) -> dict:
    """Re-enqueue the flow for the Case behind `key` (audit WF-5). Auth +
    tenant-scoped: only works when `key` resolves to a run in the caller's
    tenant. Returns {job_id, flow_id, sf_id, trigger}."""
    from interpreter import jobs as _jobs
    from interpreter.sf_ingest import enqueue_case_run

    key = key.strip()
    S = _service
    my_tenants = {
        str(r["tenant_id"]) for r in
        (c.sb.table("tenant_members").select("tenant_id").eq("user_id", c.user_id)
         .execute().data or [])
    }
    rows = (_q(lambda: S.table("runs").select("*").eq("run_id", key).execute().data)
            + _q(lambda: S.table("runs").select("*").eq("case_id", key).execute().data)
            + _q(lambda: S.table("runs").select("*").eq("case_payload->>sf_id", key).execute().data)
            + _q(lambda: S.table("runs").select("*").eq("case_payload->>case_number", key).execute().data))
    rows = [r for r in rows if str(r.get("tenant_id")) in my_tenants]
    if not rows:
        raise HTTPException(404, f"no run in your tenant for {key!r}")
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    r = rows[0]
    cp = r.get("case_payload") or {}
    sf_id = cp.get("sf_id") or cp.get("id") or r.get("case_id")
    if not sf_id or not str(sf_id).startswith("500"):
        raise HTTPException(422, "this run has no Salesforce Case id to re-run")
    ik = f"retry:{sf_id}:{int(time.time())}"
    jid = enqueue_case_run(S, sf_id, dedupe_key=ik, idempotency_key=ik,
                           trigger="retry", flow_id=r.get("flow_id"))
    log.info("trace retry by %s: case %s -> job %s", c.user_id, sf_id, jid)
    return {"job_id": jid, "flow_id": r.get("flow_id"), "sf_id": sf_id, "trigger": "retry"}


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


# Every tenant has one canonical org-level KB collection — the default target
# for connected sources, and what a chat flow's `retrieve` node reads when it
# names no `kb_sources`. Team collections are optional on top of it.
_ORG_KB_NAME = "Organization knowledge"


def _ensure_org_kb(tenant_id: str) -> dict:
    """Find (or lazily create) the tenant's org KB collection. Never promotes
    an existing team collection — a tenant that already had collections just
    gets the org KB added alongside."""
    rows = (_service.table("sources").select("source_id, name, config, created_at")
            .eq("kind", "internal_kb").eq("tenant_id", tenant_id)
            .neq("status", "archived").execute().data or [])
    for s in rows:
        if (s.get("config") or {}).get("org_kb"):
            return s
    return (_service.table("sources").insert({
        "kind": "internal_kb", "tenant_id": tenant_id, "name": _ORG_KB_NAME,
        "config": {"org_kb": True,
                   "description": "Every connected knowledge source feeds this. "
                                  "Your chat flows read all of it by default."},
    }).execute().data)[0]


@app.get("/api/kb/collections")
def kb_list_collections(tenant_id: str | None = None, c: Caller = Depends(caller)) -> list[dict]:
    tid = _caller_tenant(c, tenant_id)
    try:
        _ensure_org_kb(tid)
    except Exception as e:  # noqa: BLE001
        log.warning("ensure_org_kb(%s): %s", tid, e)

    cols = (c.sb.table("sources").select("*")
            .eq("kind", "internal_kb").eq("tenant_id", tid)
            .neq("status", "archived").execute().data or [])
    out = []
    for s in cols:
        entries = (c.sb.table("kb_entries").select("entry_id, status")
                   .eq("source_id", s["source_id"]).execute().data or [])
        active = [e for e in entries if e["status"] == "active"]
        provisional = [e for e in entries if e["status"] == "provisional"]
        out.append({
            "source_id": s["source_id"], "name": s["name"],
            "description": (s.get("config") or {}).get("description"),
            "tenant_id": s["tenant_id"], "entry_count": len(active),
            "provisional_count": len(provisional),
            "org_kb": bool((s.get("config") or {}).get("org_kb")),
            "created_at": s.get("created_at"),
        })
    out.sort(key=lambda r: (not r["org_kb"], (r["name"] or "").lower()))
    return out


@app.get("/api/kb/connections")
def kb_list_all_connections(tenant_id: str | None = None, c: Caller = Depends(caller)) -> list[dict]:
    """Every connected source for the caller's tenant, across all collections
    — the org-wide "what feeds the KB" view (onboarding + the Knowledge
    panel's org section)."""
    tid = _caller_tenant(c, tenant_id)
    conns = (c.sb.table("kb_source_connections").select("*")
             .eq("tenant_id", tid).neq("status", "archived")
             .order("created_at").execute().data or [])
    col_names = {s["source_id"]: s["name"] for s in
                 (c.sb.table("sources").select("source_id, name")
                  .eq("kind", "internal_kb").execute().data or [])}
    counts: dict[str, int] = {}
    for e in (c.sb.table("kb_entries").select("connection_id, status")
              .eq("tenant_id", tid).execute().data or []):
        cid = e.get("connection_id")
        if cid and e["status"] == "active":
            counts[cid] = counts.get(cid, 0) + 1
    for r in conns:
        r["collection_name"] = col_names.get(r["source_id"])
        r["entry_count"] = counts.get(r["connection_id"], 0)
    return conns


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

    from interpreter import audit
    audit.record(_service, tenant_id=tenant_id, action="kb_collection.created",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="kb_collection", target_id=created["source_id"],
                 summary=f"created KB collection {body.name!r}")
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
    updated = c.sb.table("sources").update(patch).eq("source_id", sid).execute().data[0]

    from interpreter import audit
    audit.record(_service, tenant_id=col["tenant_id"], action="kb_collection.updated",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="kb_collection", target_id=sid,
                 summary=f"updated KB collection {updated.get('name', sid)!r}")
    return updated


@app.delete("/api/kb/collections/{sid}", status_code=204)
def kb_delete_collection(sid: str, c: Caller = Depends(caller)) -> None:
    rate_limit(c.user_id, "kb_write", 60)
    col = _kb_collection(c, sid)
    _require_editor(c, col["tenant_id"])   # RLS + role gate
    c.sb.table("sources").update({"status": "archived"}).eq("source_id", sid).execute()
    entries = (c.sb.table("kb_entries").select("entry_id")
               .eq("source_id", sid).eq("status", "active").execute().data or [])
    for e in entries:
        c.sb.table("kb_entries").update({"status": "archived"}).eq("entry_id", e["entry_id"]).execute()
        _kb_delete(_service, url=_kb_url(sid, e["entry_id"]))

    from interpreter import audit
    audit.record(_service, tenant_id=col["tenant_id"], action="kb_collection.deleted",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="kb_collection", target_id=sid,
                 summary=f"deleted KB collection {col.get('name', sid)!r} ({len(entries)} entries archived)")


@app.get("/api/kb/collections/{sid}/entries")
def kb_list_entries(sid: str, c: Caller = Depends(caller)) -> list[dict]:
    _kb_collection(c, sid)
    rows = (c.sb.table("kb_entries")
            .select("entry_id, title, status, chunk_count, embedded_at, updated_at, "
                    "updated_by, origin, quality, gdoc_url, gsheet_id, gsheet_range, gsheet_row, "
                    "synced_at, sync_error, "
                    "provisional_until, supersedes_entry_id, source_review_task")
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

    from interpreter import audit
    audit.record(_service, tenant_id=col["tenant_id"], action="kb_entry.created",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="kb_entry", target_id=entry["entry_id"],
                 summary=f"created KB entry {body.title!r} in {col.get('name', sid)!r}")
    return _kb_after_write(entry, col, c)


class KbUploadIn(BaseModel):
    filename: str
    content_b64: str        # raw file bytes, base64 (a data: URL prefix is stripped)


@app.post("/api/kb/collections/{sid}/upload", status_code=201)
def kb_upload_file(sid: str, body: KbUploadIn, c: Caller = Depends(caller)) -> dict:
    """P7b — a .pdf / .docx / .md / .txt upload becomes a KB entry (text
    extracted, then chunked + embedded like any other entry)."""
    import base64 as _b64
    from interpreter import fileimport

    rate_limit(c.user_id, "kb_write", 60)
    col = _kb_collection(c, sid)
    _require_editor(c, col["tenant_id"])
    raw = body.content_b64.split(",", 1)[-1]        # tolerate a data:...;base64, prefix
    try:
        data = _b64.b64decode(raw, validate=False)
    except Exception:  # noqa: BLE001
        raise HTTPException(422, "content_b64 is not valid base64")
    try:
        title, text = fileimport.extract(body.filename, data)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(422, str(e))
    entry = c.sb.table("kb_entries").insert({
        "source_id": sid, "tenant_id": col["tenant_id"], "title": title,
        "body_md": text, "origin": "file",
        "created_by": c.user_id, "updated_by": c.user_id,
    }).execute().data[0]

    from interpreter import audit
    audit.record(_service, tenant_id=col["tenant_id"], action="kb_entry.created",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="kb_entry", target_id=entry["entry_id"],
                 summary=f"uploaded {body.filename!r} as a KB entry in {col.get('name', sid)!r}")
    return _kb_after_write(entry, col, c)


class KbCrawlIn(BaseModel):
    url: str
    max_pages: int = 20


# ── KB source connections (docs/KB_SOURCE_CONNECTORS.md) ───────────────────
# A "connected feed" (a crawl root, a Google Sheet/Doc, later Linear/Discourse/
# Nolt) is a first-class `kb_source_connections` row, not an array buried in
# `sources.config`. Every connector plugs in through one registry
# (`interpreter/kb_connectors.py`), one generic sync job (`kb_sync`), and these
# routes — no per-connector endpoint. `/crawl`, `/gdoc`, `/gsheet`, `/resync`
# below are kept as thin wrappers over this so old callers don't break.

class KbConnectionIn(BaseModel):
    connector: str
    config: dict[str, Any] = {}
    label: str | None = None


class KbConnectionPatch(BaseModel):
    status: str | None = None            # 'active' | 'paused'
    label: str | None = None
    config: dict[str, Any] | None = None  # partial — merged onto the stored config, re-normalized


class KbDocDefaultsIn(BaseModel):
    tenant_id: str | None = None
    index: bool | None = None
    on_correction: str | None = None      # 'off' | 'suggest' | 'write_back'
    github_repo: str | None = None


_KB_DOC_SYSTEM_DEFAULTS = {"index": True, "on_correction": "off"}


def _validate_kb_doc_defaults(body: "KbDocDefaultsIn") -> dict:
    """The stored blob is *partial* — only the keys the org actually set.
    Same rules as a per-connection gdocs config: `on_correction` in the
    enum, and if it's not 'off' the default must also carry a valid
    `github_repo` and not force `index` off."""
    out: dict[str, Any] = {}
    if body.index is not None:
        out["index"] = bool(body.index)
    oc = (body.on_correction or "").strip()
    if oc:
        if oc not in ("off", "suggest", "write_back"):
            raise HTTPException(422, "on_correction must be 'off', 'suggest' or 'write_back'")
        out["on_correction"] = oc
    repo = (body.github_repo or "").strip()
    if repo:
        if repo.count("/") != 1 or not all(repo.split("/")):
            raise HTTPException(422, "github_repo must be 'owner/name'")
        out["github_repo"] = repo
    if out.get("on_correction", "off") != "off":
        if out.get("index") is False:
            raise HTTPException(422, "a 'suggest' / 'write_back' default needs index = true")
        if "github_repo" not in out:
            raise HTTPException(422, "a 'suggest' / 'write_back' default needs a github_repo")
    return out


def _kb_doc_defaults(tenant_id: str) -> dict:
    rows = (_service.table("tenants").select("kb_doc_defaults")
            .eq("tenant_id", tenant_id).execute().data or [])
    return (rows[0].get("kb_doc_defaults") if rows else None) or {}


@app.get("/api/kb/doc-defaults")
def kb_get_doc_defaults(tenant_id: str | None = None, c: Caller = Depends(caller)) -> dict:
    """The org-level default for a new Google Doc connection's two knobs.
    `stored` is what was set ({} = none); `effective` = system defaults with
    `stored` layered on."""
    tid = _caller_tenant(c, tenant_id)
    stored = _kb_doc_defaults(tid)
    return {"tenant_id": tid, "stored": stored,
            "effective": {**_KB_DOC_SYSTEM_DEFAULTS, **stored}}


@app.put("/api/kb/doc-defaults")
def kb_set_doc_defaults(body: KbDocDefaultsIn, c: Caller = Depends(caller)) -> dict:
    tid = _caller_tenant(c, body.tenant_id)
    _require_editor(c, tid)
    rate_limit(c.user_id, "kb_write", 60)
    clean = _validate_kb_doc_defaults(body)
    updated = (_service.table("tenants").update({"kb_doc_defaults": clean})
               .eq("tenant_id", tid).execute().data)
    if not updated:   # a pre-`tenants`-table tenant (see set_case_connector)
        _service.table("tenants").upsert(
            {"tenant_id": tid, "name": f"workspace {tid[:8]}", "kb_doc_defaults": clean}
        ).execute()

    from interpreter import audit
    audit.record(_service, tenant_id=tid, action="kb.doc_defaults_changed",
                 actor_id=c.user_id, actor_email=c.email, target_type="tenant", target_id=tid,
                 summary=f"Google-Doc defaults set to {clean or '(cleared)'}")
    return {"tenant_id": tid, "stored": clean,
            "effective": {**_KB_DOC_SYSTEM_DEFAULTS, **clean}}


def _kb_connection(c: Caller, cid: str) -> dict:
    rows = (c.sb.table("kb_source_connections").select("*")
            .eq("connection_id", cid).neq("status", "archived").execute().data or [])
    if not rows:
        raise HTTPException(404, "connection not found or not visible to you")
    return rows[0]


def _kb_pull_secrets(tenant_id: str, connector: str, spec, raw_config: dict) -> dict:
    """Pop any `secret: true` config field (an API key) out of `raw_config`
    into Supabase Vault under the connector kind — merged with any prior
    value, so a blank field reuses the saved one — and return `raw_config`
    without it. A secret must never reach the `kb_source_connections.config`
    row (returned to the browser)."""
    from interpreter import vault_secrets

    raw = dict(raw_config or {})
    secrets = {f["key"]: str(raw.pop(f["key"], "")).strip()
               for f in spec.config_fields if f.get("secret")}
    secrets = {k: v for k, v in secrets.items() if v}
    if secrets:
        prior = vault_secrets.get(tenant_id, connector, sb=_service)
        vault_id = vault_secrets.put(tenant_id, connector, {**prior, **secrets}, sb=_service)
        row = {"tenant_id": tenant_id, "kind": connector, "secret": {"has_credentials": True}}
        if vault_id:
            row["vault_secret_id"] = vault_id   # match the slack/llm integration shape
        _service.table("tenant_integrations").upsert(row).execute()
    return raw


def _kb_add_connection(c: Caller, col: dict, connector: str, raw_config: dict,
                       label: str | None) -> dict:
    """Validate + create a connection row and kick off its first sync. Shared
    by POST /connections and the /crawl,/gdoc,/gsheet wrappers."""
    from interpreter import audit
    from interpreter.kb_connectors import get_kb_connector

    try:
        spec = get_kb_connector(connector)
    except KeyError:
        raise HTTPException(422, f"unknown connector {connector!r}")
    raw_config = dict(raw_config or {})
    # gdocs inherits the tenant's org-level default for any knob the caller
    # didn't set explicitly (the "+ add source" form pre-fills from it, so
    # this is the backstop for API callers / form gaps).
    if connector == "gdocs":
        raw_config = {**_kb_doc_defaults(col["tenant_id"]), **raw_config}

    raw_config = _kb_pull_secrets(col["tenant_id"], connector, spec, raw_config)

    ok, reason = spec.is_available(col["tenant_id"], _service)
    if not ok:
        raise HTTPException(400, reason or f"{spec.label} is not available")
    try:
        config = spec.normalize_config(raw_config)
    except ValueError as e:
        raise HTTPException(422, str(e))

    row = c.sb.table("kb_source_connections").insert({
        "source_id": col["source_id"], "tenant_id": col["tenant_id"],
        "connector": connector, "config": config,
        "label": (label or "").strip() or config.get("url") or config.get("doc_url")
                 or config.get("sheet_id") or spec.label,
        "created_by": c.user_id,
    }).execute().data[0]
    jobs.enqueue("kb_sync", {"connection_id": row["connection_id"],
                             "collection_name": col["name"]},
                 dedupe_key=f"kb_sync:{row['connection_id']}", sb=_service)
    audit.record(_service, tenant_id=col["tenant_id"], action="kb_connection.created",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="kb_connection", target_id=row["connection_id"],
                 summary=f"connected {spec.label} to KB collection "
                         f"{col.get('name', col['source_id'])!r}")
    return row


@app.get("/api/kb/connectors")
def kb_list_connectors(tenant_id: str | None = None, c: Caller = Depends(caller)) -> list[dict]:
    """The KB source-connector catalogue for the '+ add source' form, each
    with whether its auth prerequisite is met for this tenant."""
    from interpreter.kb_connectors import list_kb_connectors

    tid = _caller_tenant(c, tenant_id)
    out = []
    for spec in list_kb_connectors():
        ok, why = spec.is_available(tid, _service)
        out.append({"slug": spec.slug, "label": spec.label, "auth": spec.auth,
                    "config_fields": spec.config_fields, "available": ok, "reason": why})
    return out


class KbConnectorTestIn(BaseModel):
    tenant_id: str | None = None
    api_key: str | None = None
    board_id: str | None = None
    base_url: str | None = None
    api_username: str | None = None


@app.post("/api/kb/connectors/{slug}/test")
def kb_test_connector(slug: str, body: KbConnectorTestIn, c: Caller = Depends(caller)) -> dict:
    """Lightweight authed read against an apikey connector's API — saves
    nothing. Uses the posted `api_key` (a not-yet-saved key from the form)
    or falls back to the stored one. -> {ok, detail}."""
    tid = _caller_tenant(c, body.tenant_id)
    _require_editor(c, tid)
    rate_limit(c.user_id, "integration", 20)
    key = (body.api_key or "").strip() or None
    if slug == "linear":
        from interpreter import linear
        return linear.test_connection(tid, _service, api_key=key)
    if slug == "nolt":
        from interpreter import nolt
        if not (body.board_id or "").strip():
            raise HTTPException(422, "board_id is required to test Nolt")
        return nolt.test_connection(tid, _service, body.board_id.strip(), api_key=key)
    if slug == "discourse":
        from interpreter import discourse
        if not (body.base_url or "").strip():
            raise HTTPException(422, "base_url is required to test a Discourse forum")
        return discourse.test_connection(tid, _service, body.base_url.strip(),
                                         api_key=key, api_username=(body.api_username or None))
    raise HTTPException(422, f"no connection test for connector {slug!r}")


@app.get("/api/kb/collections/{sid}/connections")
def kb_list_connections(sid: str, c: Caller = Depends(caller)) -> list[dict]:
    _kb_collection(c, sid)
    conns = (c.sb.table("kb_source_connections").select("*")
             .eq("source_id", sid).neq("status", "archived")
             .order("created_at").execute().data or [])
    counts: dict[str, int] = {}
    for e in (c.sb.table("kb_entries").select("connection_id, status")
              .eq("source_id", sid).execute().data or []):
        cid = e.get("connection_id")
        if cid and e["status"] == "active":
            counts[cid] = counts.get(cid, 0) + 1
    for r in conns:
        r["entry_count"] = counts.get(r["connection_id"], 0)
    return conns


@app.post("/api/kb/collections/{sid}/connections", status_code=202)
def kb_create_connection(sid: str, body: KbConnectionIn, c: Caller = Depends(caller)) -> dict:
    rate_limit(c.user_id, "kb_write", 60)
    col = _kb_collection(c, sid)
    _require_editor(c, col["tenant_id"])
    return _kb_add_connection(c, col, body.connector, body.config, body.label)


@app.post("/api/kb/connections/{cid}/sync", status_code=202)
def kb_sync_connection(cid: str, c: Caller = Depends(caller)) -> dict:
    rate_limit(c.user_id, "kb_write", 60)
    conn = _kb_connection(c, cid)
    _require_editor(c, conn["tenant_id"])
    job_id = jobs.enqueue("kb_sync", {"connection_id": cid},
                          dedupe_key=f"kb_sync:{cid}", sb=_service)
    return {"job_id": job_id, "deduped": job_id is None}


@app.patch("/api/kb/connections/{cid}")
def kb_update_connection(cid: str, body: KbConnectionPatch, c: Caller = Depends(caller)) -> dict:
    """Change a connection's status (pause/resume), label, and/or `config`.
    A `config` patch is merged onto the stored config and re-normalized
    through the connector's spec, then a re-sync is kicked off. Secret
    fields (an API key) are routed to Vault, not the row."""
    conn = _kb_connection(c, cid)
    _require_editor(c, conn["tenant_id"])
    if body.status not in (None, "active", "paused"):
        raise HTTPException(422, "status must be 'active' or 'paused'")

    patch: dict[str, Any] = {}
    resync = False
    if body.status is not None:
        patch["status"] = body.status
    if body.label is not None:
        patch["label"] = body.label.strip()

    if body.config is not None:
        from interpreter.kb_connectors import get_kb_connector
        try:
            spec = get_kb_connector(conn["connector"])
        except KeyError:
            raise HTTPException(422, f"unknown connector {conn['connector']!r}")
        rate_limit(c.user_id, "kb_write", 60)
        raw = _kb_pull_secrets(conn["tenant_id"], conn["connector"], spec, body.config)
        merged = {**(conn.get("config") or {}), **raw}
        try:
            patch["config"] = spec.normalize_config(merged)
        except ValueError as e:
            raise HTTPException(422, str(e))
        patch["status"] = "active"   # a config edit re-activates + re-syncs
        resync = True

    if not patch:
        return conn
    updated = (c.sb.table("kb_source_connections").update(patch)
               .eq("connection_id", cid).execute().data[0])
    if resync or patch.get("status") == "active":
        jobs.enqueue("kb_sync", {"connection_id": cid},
                     dedupe_key=f"kb_sync:{cid}", sb=_service)
    if resync:
        from interpreter import audit
        audit.record(_service, tenant_id=conn["tenant_id"], action="kb_connection.updated",
                     actor_id=c.user_id, actor_email=c.email,
                     target_type="kb_connection", target_id=cid,
                     summary=f"edited the {conn.get('connector')} connection's config")
    return updated


@app.delete("/api/kb/connections/{cid}", status_code=204)
def kb_delete_connection(cid: str, c: Caller = Depends(caller)) -> None:
    from interpreter import audit

    conn = _kb_connection(c, cid)
    _require_editor(c, conn["tenant_id"])
    c.sb.table("kb_source_connections").update({"status": "archived"}) \
        .eq("connection_id", cid).execute()
    entries = (c.sb.table("kb_entries").select("entry_id, source_id")
               .eq("connection_id", cid).neq("status", "archived").execute().data or [])
    for e in entries:
        c.sb.table("kb_entries").update({"status": "archived"}) \
            .eq("entry_id", e["entry_id"]).execute()
        _kb_delete(_service, url=_kb_url(e["source_id"], e["entry_id"]))
    audit.record(_service, tenant_id=conn["tenant_id"], action="kb_connection.deleted",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="kb_connection", target_id=cid,
                 summary=f"disconnected {conn.get('connector')} from a KB collection "
                         f"({len(entries)} entries archived)")


@app.get("/api/kb/collections/{sid}/doc-writebacks")
def kb_list_doc_writebacks(sid: str, c: Caller = Depends(caller)) -> list[dict]:
    """KB write-back audit (docs/KB_SOURCE_CONNECTORS.md §2) — the automated
    Google-Doc edits made after a KIL correction was approved, for this
    collection's connections, newest first."""
    _kb_collection(c, sid)
    cids = [r["connection_id"] for r in
            (c.sb.table("kb_source_connections").select("connection_id")
             .eq("source_id", sid).execute().data or [])]
    if not cids:
        return []
    return (c.sb.table("kb_doc_writebacks").select("*")
            .in_("connection_id", cids).order("applied_at", desc=True)
            .execute().data or [])


_KB_WB_OPEN = ("suggested", "applied", "partial", "conflict")


@app.get("/api/kb/doc-writebacks")
def kb_list_all_doc_writebacks(tenant_id: str | None = None, status: str = "open",
                               c: Caller = Depends(caller)) -> list[dict]:
    """Tenant-wide KB write-back audit for the review UI. `status`: 'open'
    (still awaiting a human on GitHub) or 'all'. Each row is enriched with
    its connection's label + doc url."""
    tid = _caller_tenant(c, tenant_id)
    q = (c.sb.table("kb_doc_writebacks").select("*").eq("tenant_id", tid)
         .order("applied_at", desc=True).limit(200))
    if status != "all":
        q = q.in_("status", list(_KB_WB_OPEN))
    rows = q.execute().data or []
    conns = {
        r["connection_id"]: r for r in
        (c.sb.table("kb_source_connections")
         .select("connection_id, label, config")
         .eq("tenant_id", tid).execute().data or [])
    }
    for r in rows:
        cn = conns.get(r.get("connection_id")) or {}
        r["connection_label"] = cn.get("label")
        r["doc_url"] = (cn.get("config") or {}).get("doc_url")
    return rows


@app.post("/api/kb/collections/{sid}/crawl", status_code=202)
def kb_crawl_site(sid: str, body: KbCrawlIn, c: Caller = Depends(caller)) -> dict:
    """Deprecated — thin wrapper over POST /connections {connector:"public_url"}."""
    rate_limit(c.user_id, "kb_write", 60)
    col = _kb_collection(c, sid)
    _require_editor(c, col["tenant_id"])
    return _kb_add_connection(c, col, "public_url",
                              {"url": body.url, "max_pages": body.max_pages}, None)


# ── Phase 28 step 6: bulk KB export/import ──────────────────────────────
@app.get("/api/kb/collections/{sid}/export")
def kb_export_collection(sid: str, c: Caller = Depends(caller)) -> dict:
    """A downloadable JSON backup of this collection's current entries
    (active + provisional -- not archived/superseded). Re-importable as-is."""
    from interpreter import kb_backup

    col = _kb_collection(c, sid)
    rows = (c.sb.table("kb_entries").select("title, body_md, status")
            .eq("source_id", sid).in_("status", ["active", "provisional"])
            .order("title").execute().data or [])
    return kb_backup.export_bundle(col, rows)


class KbImportIn(BaseModel):
    entries: list[dict[str, Any]]


@app.post("/api/kb/collections/{sid}/import", status_code=202)
def kb_import_bundle(sid: str, body: KbImportIn, c: Caller = Depends(caller)) -> dict:
    """Bulk-restore entries from an export bundle (or any {title, body_md}[]
    JSON). Async — the worker creates + embeds each entry, matching /crawl's
    pattern. An entry whose title already exists in this collection (from a
    prior import) is updated in place, not duplicated."""
    from interpreter import audit, kb_backup

    col = _kb_collection(c, sid)
    _require_editor(c, col["tenant_id"])
    rate_limit(c.user_id, "kb_write", 60)
    clean, warnings = kb_backup.normalize_import_entries(body.entries)
    if not clean:
        raise HTTPException(422, {"error": "no valid entries", "warnings": warnings})
    job_id = jobs.enqueue("import_kb_bundle", {
        "source_id": sid, "tenant_id": col["tenant_id"], "collection_name": col["name"],
        "entries": clean, "created_by": c.user_id,
    }, dedupe_key=f"import:{sid}:{uuid.uuid4()}", sb=_service)
    audit.record(_service, tenant_id=col["tenant_id"], action="kb.import_started",
                actor_id=c.user_id, actor_email=c.email,
                target_type="kb_collection", target_id=sid,
                summary=f"importing {len(clean)} entries into {col['name']}",
                metadata={"count": len(clean), "warnings": warnings})
    return {"job_id": job_id, "accepted": len(clean), "warnings": warnings}


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
    from interpreter import audit
    audit.record(_service, tenant_id=col["tenant_id"], action="kb_entry.updated",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="kb_entry", target_id=eid,
                 summary=f"updated KB entry {updated.get('title', eid)!r}",
                 metadata={"body_changed": body_changed})
    return _kb_after_write(updated, col, c, force=body_changed, title_only=not body_changed)


@app.delete("/api/kb/entries/{eid}", status_code=204)
def kb_delete_entry(eid: str, c: Caller = Depends(caller)) -> None:
    rate_limit(c.user_id, "kb_write", 60)
    entry = _kb_entry(c, eid)
    _require_editor(c, entry["tenant_id"])
    c.sb.table("kb_entries").update({"status": "archived", "updated_by": c.user_id}) \
        .eq("entry_id", eid).execute()
    _kb_delete(_service, url=_kb_url(entry["source_id"], eid))

    from interpreter import audit
    audit.record(_service, tenant_id=entry["tenant_id"], action="kb_entry.deleted",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="kb_entry", target_id=eid,
                 summary=f"deleted KB entry {entry.get('title', eid)!r}")


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
    from interpreter import vault_secrets

    vault_id = vault_secrets.put(tenant_id, "google", {
        "refresh_token": tok["refresh_token"], "scope": tok.get("scope"),
    }, sb=_service)
    row = {"tenant_id": tenant_id, "kind": "google",
           "secret": {"scope": tok.get("scope"), "has_credentials": True}}
    if vault_id:
        row["vault_secret_id"] = vault_id
    _service.table("tenant_integrations").upsert(row).execute()
    return page("Google connected. You can close this window.")


@app.post("/api/kb/collections/{sid}/gdoc", status_code=202)
def kb_link_gdoc(sid: str, body: GdocLinkIn, c: Caller = Depends(caller)) -> dict:
    """Deprecated — thin wrapper over POST /connections {connector:"gdocs"}."""
    rate_limit(c.user_id, "kb_write", 60)
    col = _kb_collection(c, sid)
    _require_editor(c, col["tenant_id"])
    return _kb_add_connection(c, col, "gdocs", {"doc_url": body.doc_url}, None)


class GsheetLinkIn(BaseModel):
    sheet_url: str
    sheet_name: str | None = None


@app.post("/api/kb/collections/{sid}/gsheet", status_code=202)
def kb_link_gsheet(sid: str, body: GsheetLinkIn, c: Caller = Depends(caller)) -> dict:
    """Deprecated — thin wrapper over POST /connections {connector:"gsheets"}."""
    rate_limit(c.user_id, "kb_write", 60)
    col = _kb_collection(c, sid)
    _require_editor(c, col["tenant_id"])
    return _kb_add_connection(c, col, "gsheets",
                              {"sheet_url": body.sheet_url, "sheet_name": body.sheet_name}, None)


@app.post("/api/kb/entries/{eid}/resync", status_code=202)
def kb_resync_gdoc(eid: str, c: Caller = Depends(caller)) -> dict:
    """Re-sync the connection that produced this entry (crawl / gsheet / gdoc).
    A connection re-sync re-fetches every document the feed holds, so a single
    entry's resync button just kicks off its whole connection."""
    rate_limit(c.user_id, "kb_write", 60)
    entry = _kb_entry(c, eid)
    cid = entry.get("connection_id")
    if not cid:
        raise HTTPException(400, "not a connected (synced) entry")
    col = _kb_collection(c, entry["source_id"])
    _require_editor(c, col["tenant_id"])
    job_id = jobs.enqueue("kb_sync", {"connection_id": cid, "collection_name": col["name"]},
                          dedupe_key=f"kb_sync:{cid}", sb=_service)
    return {"job_id": job_id, "deduped": job_id is None}


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

    from interpreter import audit
    audit.record(_service, tenant_id=tid,
                 action="email_channel.configured" if existing else "email_channel.connected",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="email_channel", target_id=tid,
                 summary=f"{'updated' if existing else 'connected'} the {body.provider} mailbox")
    return email_status(tenant_id=tid, c=c)


@app.delete("/api/integrations/email", status_code=204)
def email_disconnect(tenant_id: str | None = None, c: Caller = Depends(caller)) -> None:
    tid = _caller_tenant(c, tenant_id)
    _require_owner(c, tid)
    from interpreter import audit
    from interpreter.mailbox import delete_channel

    delete_channel(tid, _service)
    audit.record(_service, tenant_id=tid, action="email_channel.disconnected",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="email_channel", target_id=tid, summary="disconnected the mailbox")


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


# ── Multi-provider connectors step 3: connect a Freshchat account ────────
FRESHCHAT_OAUTH_REDIRECT_URI = os.environ.get(
    "FRESHCHAT_OAUTH_REDIRECT_URI",
    "http://localhost:8000/api/integrations/freshchat/oauth/callback",
)


class FreshchatChannelIn(BaseModel):
    domain: str | None = None              # "yourcompany.freshchat.com"
    team: str = "support"
    api_token: str | None = None           # write-only, never returned; Vault-backed
    webhook_public_key: str | None = None  # write-only PEM, never returned
    auto_send_enabled: bool = False
    tenant_id: str | None = None
    # OAuth mode (a Custom/External App's credentials) — see
    # interpreter/freshchat.py's module docstring for why this exists
    # alongside api_token.
    oauth_domain: str | None = None
    client_id: str | None = None           # write-only, never returned; Vault-backed
    client_secret: str | None = None       # write-only, never returned; Vault-backed


def _freshchat_cfg_from_body(tenant_id: str, body: "FreshchatChannelIn", existing):
    from interpreter.freshchat import FreshchatConfig

    return FreshchatConfig(
        tenant_id=tenant_id,
        domain=(body.domain or (existing.domain if existing else "")).strip(),
        team=body.team or (existing.team if existing else "support"),
        auto_send_enabled=bool(body.auto_send_enabled),
        status=(existing.status if existing else "inactive"),
        oauth_domain=(body.oauth_domain or (existing.oauth_domain if existing else "")).strip(),
    )


@app.get("/api/integrations/freshchat")
def freshchat_status(tenant_id: str | None = None, c: Caller = Depends(caller)) -> dict:
    """Channel status for the caller's tenant. Never returns the token/key."""
    tid = _caller_tenant(c, tenant_id)
    from interpreter.freshchat import load_channel

    ch = load_channel(tid, _service)
    if not ch:
        return {"tenant_id": tid, "configured": False, "status": "none"}
    return {"tenant_id": tid, **ch.public_status()}


@app.put("/api/integrations/freshchat")
def freshchat_configure(body: FreshchatChannelIn, c: Caller = Depends(caller)) -> dict:
    tid = _caller_tenant(c, body.tenant_id)
    _require_owner(c, tid)
    rate_limit(c.user_id, "integration", 30)
    from interpreter.freshchat import load_channel, save_channel

    existing = load_channel(tid, _service)
    if not (body.domain or (existing and existing.domain)):
        raise HTTPException(422, "domain is required")
    # either auth mode is enough to save (a tenant may save client_id/secret
    # first, then complete the OAuth browser round-trip in a second step —
    # see /oauth/authorize below — before a token of either kind exists)
    has_token = bool(body.api_token) or bool(existing and existing.api_token)
    has_oauth_client = bool(body.client_id) or bool(existing and existing.client_id)
    if not (has_token or has_oauth_client):
        raise HTTPException(422, "api_token, or an OAuth client_id/client_secret, is required")

    cfg = _freshchat_cfg_from_body(tid, body, existing)
    cfg.status = "active"
    save_channel(cfg, _service, api_token=body.api_token,
                webhook_public_key=body.webhook_public_key,
                client_id=body.client_id, client_secret=body.client_secret)

    from interpreter import audit
    audit.record(_service, tenant_id=tid,
                 action="freshchat_channel.configured" if existing else "freshchat_channel.connected",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="freshchat_channel", target_id=tid,
                 summary=f"{'updated' if existing else 'connected'} the Freshchat channel")
    return freshchat_status(tenant_id=tid, c=c)


@app.get("/api/integrations/freshchat/oauth/authorize")
def freshchat_oauth_authorize(tenant_id: str | None = None, scope: str | None = None,
                              c: Caller = Depends(caller)) -> dict:
    """Start the OAuth round-trip for a tenant's Freshchat Developer-Profile
    OAuth client — save client_id/client_secret via PUT first. `scope`
    lets the caller override `oauth_authorize_url`'s default (Freshworks'
    own documented read-only example — no confirmed scope exists yet for
    sending a message or listing agents, see interpreter/freshchat.py)."""
    tid = _caller_tenant(c, tenant_id)
    _require_owner(c, tid)
    from interpreter.freshchat import load_channel, oauth_authorize_url

    cfg = load_channel(tid, _service)
    if not (cfg and cfg.client_id and cfg.client_secret):
        raise HTTPException(422, "save a client_id/client_secret before authorizing")
    if not cfg.effective_oauth_domain:
        raise HTTPException(422, "domain (or oauth_domain) is required")
    nonce = secrets.token_urlsafe(24)
    _oauth_state[nonce] = (time.time() + 600, c.user_id, tid)
    kwargs = {"scope": scope} if scope is not None else {}
    return {"url": oauth_authorize_url(cfg.effective_oauth_domain, cfg.client_id,
                                       FRESHCHAT_OAUTH_REDIRECT_URI, nonce, **kwargs)}


@app.get("/api/integrations/freshchat/oauth/callback")
def freshchat_oauth_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    def page(msg: str) -> HTMLResponse:
        return HTMLResponse(f"<!doctype html><meta charset=utf-8><p>{msg}</p>"
                            "<script>setTimeout(()=>window.close(),1500)</script>")
    if error:
        return page(f"Freshchat authorisation failed: {error}")
    hit = _oauth_state.pop(state, None)
    if not hit or hit[0] < time.time():
        return page("This authorisation link has expired — try again.")
    _, _uid, tid = hit
    from interpreter.freshchat import load_channel, oauth_exchange_code, save_channel

    cfg = load_channel(tid, _service)
    if not (cfg and cfg.client_id and cfg.client_secret):
        return page("Freshchat client_id/client_secret are no longer saved for this workspace.")
    try:
        tok = oauth_exchange_code(cfg.effective_oauth_domain, cfg.client_id, cfg.client_secret,
                                  code, FRESHCHAT_OAUTH_REDIRECT_URI)
    except Exception as e:  # noqa: BLE001
        return page(f"Could not complete Freshchat OAuth: {e}")
    save_channel(cfg, _service, refresh_token=tok["refresh_token"])

    from interpreter import audit
    audit.record(_service, tenant_id=tid, action="freshchat_channel.oauth_connected",
                 target_type="freshchat_channel", target_id=tid,
                 summary="completed the Freshchat OAuth authorization")
    return page("Freshchat connected. You can close this window.")


@app.delete("/api/integrations/freshchat", status_code=204)
def freshchat_disconnect(tenant_id: str | None = None, c: Caller = Depends(caller)) -> None:
    tid = _caller_tenant(c, tenant_id)
    _require_owner(c, tid)
    from interpreter import audit
    from interpreter.freshchat import delete_channel

    delete_channel(tid, _service)
    audit.record(_service, tenant_id=tid, action="freshchat_channel.disconnected",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="freshchat_channel", target_id=tid,
                 summary="disconnected the Freshchat channel")


@app.post("/api/integrations/freshchat/test")
def freshchat_test(body: FreshchatChannelIn, c: Caller = Depends(caller)) -> dict:
    """A lightweight authenticated read — saves nothing. Uses the posted
    token, falling back to the stored one when the field is left blank."""
    tid = _caller_tenant(c, body.tenant_id)
    _require_owner(c, tid)
    rate_limit(c.user_id, "integration", 20)
    from interpreter.freshchat import load_channel, test_connection

    existing = load_channel(tid, _service)
    cfg = _freshchat_cfg_from_body(tid, body, existing)
    cfg.api_token = body.api_token or (existing.api_token if existing else "")
    cfg.client_id = body.client_id or (existing.client_id if existing else "")
    cfg.client_secret = body.client_secret or (existing.client_secret if existing else "")
    cfg.refresh_token = existing.refresh_token if existing else ""
    return test_connection(cfg, sb=_service)


@app.get("/api/integrations/freshchat/webhook-url")
def freshchat_webhook_url(tenant_id: str | None = None, c: Caller = Depends(caller)) -> dict:
    """The URL to paste into Freshchat's own webhook settings for this
    tenant — a thin convenience so the web UI doesn't hardcode _public_base()."""
    tid = _caller_tenant(c, tenant_id)
    return {"url": f"{_public_base()}/webhooks/freshchat/{tid}"}


# ── Product analytics: PostHog (Phase 30, docs/PRODUCT_ANALYTICS_CONNECTOR.md) ──
class PostHogIn(BaseModel):
    tenant_id: str | None = None
    host: str | None = None
    project_id: str | None = None
    milestone_events: list[str] | None = None
    api_key: str | None = None


def _posthog_cfg_from_body(tid: str, body: PostHogIn, existing) -> "object":
    from interpreter.posthog import PostHogConfig, _clean_host, _clean_milestones
    return PostHogConfig(
        tenant_id=tid,
        host=_clean_host(body.host if body.host is not None
                         else (existing.host if existing else None)),
        project_id=str(body.project_id if body.project_id is not None
                       else (existing.project_id if existing else "")),
        milestone_events=_clean_milestones(
            body.milestone_events if body.milestone_events is not None
            else (existing.milestone_events if existing else [])),
    )


@app.get("/api/integrations/posthog")
def posthog_status(tenant_id: str | None = None, c: Caller = Depends(caller)) -> dict:
    """PostHog connection status for the caller's tenant. Never returns the
    key. `coverage_pct` / `contacts_synced` / `last_synced_at` come from the
    last `product_analytics_sync` run (graph_sync_state)."""
    tid = _caller_tenant(c, tenant_id)
    from interpreter.posthog import load
    cfg = load(tid, _service)
    if not cfg:
        return {"tenant_id": tid, "configured": False, "status": "none"}
    sync = (_service.table("graph_sync_state")
            .select("coverage_pct, contacts_synced, last_run_at")
            .eq("scope", f"product_analytics:{tid}").limit(1).execute().data or [{}])[0]
    return {"tenant_id": tid, **cfg.public_status(),
            "coverage_pct": sync.get("coverage_pct"),
            "contacts_synced": sync.get("contacts_synced"),
            "last_synced_at": sync.get("last_run_at")}


@app.put("/api/integrations/posthog")
def posthog_configure(body: PostHogIn, c: Caller = Depends(caller)) -> dict:
    tid = _caller_tenant(c, body.tenant_id)
    _require_owner(c, tid)
    rate_limit(c.user_id, "integration", 30)
    from interpreter.posthog import load, save

    existing = load(tid, _service)
    cfg = _posthog_cfg_from_body(tid, body, existing)
    if not cfg.project_id:
        raise HTTPException(422, "project_id is required")
    has_key = bool(body.api_key) or bool(existing and existing.has_credentials)
    if not has_key:
        raise HTTPException(422, "an api_key is required")
    cfg.status = "active"
    save(cfg, _service, api_key=body.api_key)

    from interpreter import audit
    audit.record(_service, tenant_id=tid,
                 action="posthog.configured" if existing else "posthog.connected",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="integration", target_id=tid,
                 summary=f"{'updated' if existing else 'connected'} the PostHog connector")
    return posthog_status(tenant_id=tid, c=c)


@app.delete("/api/integrations/posthog", status_code=204)
def posthog_disconnect(tenant_id: str | None = None, c: Caller = Depends(caller)) -> None:
    tid = _caller_tenant(c, tenant_id)
    _require_owner(c, tid)
    from interpreter import audit
    from interpreter.posthog import delete
    delete(tid, _service)
    audit.record(_service, tenant_id=tid, action="posthog.disconnected",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="integration", target_id=tid,
                 summary="disconnected the PostHog connector")


@app.post("/api/integrations/posthog/test")
def posthog_test(body: PostHogIn, c: Caller = Depends(caller)) -> dict:
    """A cheap `SELECT 1` HogQL query — saves nothing. Uses the posted key,
    falling back to the stored one when the field is blank."""
    tid = _caller_tenant(c, body.tenant_id)
    _require_owner(c, tid)
    rate_limit(c.user_id, "integration", 20)
    from interpreter.posthog import load, test_connection

    existing = load(tid, _service)
    cfg = _posthog_cfg_from_body(tid, body, existing)
    return test_connection(tid, _service, host=cfg.host, project_id=cfg.project_id,
                           api_key=body.api_key or None)


# ── BYOK: self-serve LLM provider keys + model roster (chunk 3 of the
#    2026-09-04 onboarding/robustness work) ─────────────────────────────
_LLM_PROVIDERS = ("groq", "anthropic", "openrouter")


class LlmKeyIn(BaseModel):
    provider: str
    api_key: str
    tenant_id: str | None = None


@app.get("/api/integrations/llm")
def llm_key_status(tenant_id: str | None = None, c: Caller = Depends(caller)) -> dict:
    """Which providers this tenant has their own key for, and which the
    platform itself has a fallback key for (so the UI can say "Groq already
    works, nothing to do" instead of implying every provider needs setup).
    Never returns a key — only booleans."""
    tid = _caller_tenant(c, tenant_id)
    from interpreter import llm as llmmod, vault_secrets

    secret = vault_secrets.get(tid, "llm", sb=_service)
    return {
        "tenant_id": tid,
        "tenant": {p: bool(secret.get(f"{p}_api_key")) for p in _LLM_PROVIDERS},
        "platform": {p: bool(os.environ.get(llmmod._PROVIDER_KEY[p])) for p in _LLM_PROVIDERS},
    }


@app.put("/api/integrations/llm")
def llm_key_save(body: LlmKeyIn, c: Caller = Depends(caller)) -> dict:
    if body.provider not in _LLM_PROVIDERS:
        raise HTTPException(422, f"provider must be one of {_LLM_PROVIDERS}")
    if not body.api_key.strip():
        raise HTTPException(422, "api_key is required")
    tid = _caller_tenant(c, body.tenant_id)
    _require_owner(c, tid)
    rate_limit(c.user_id, "integration", 30)

    from interpreter import vault_secrets

    secret = dict(vault_secrets.get(tid, "llm", sb=_service))
    secret[f"{body.provider}_api_key"] = body.api_key.strip()
    vault_id = vault_secrets.put(tid, "llm", secret, sb=_service)
    row = {"tenant_id": tid, "kind": "llm", "secret": {}}
    if vault_id:
        row["vault_secret_id"] = vault_id
    _service.table("tenant_integrations").upsert(row).execute()

    # llm.py caches tenant keys for 5 min (one lookup per complete() call
    # would otherwise be a DB round trip on every LLM call) -- drop the
    # stale entry so a key just saved is picked up on the very next run.
    from interpreter import llm as llmmod
    llmmod._tenant_keys_cache.pop(tid, None)

    from interpreter import audit
    audit.record(_service, tenant_id=tid, action="llm_key.saved",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="llm_key", target_id=body.provider,
                 summary=f"set a {body.provider} API key for this workspace")
    return llm_key_status(tenant_id=tid, c=c)


@app.delete("/api/integrations/llm/{provider}", status_code=204)
def llm_key_remove(provider: str, tenant_id: str | None = None, c: Caller = Depends(caller)) -> None:
    if provider not in _LLM_PROVIDERS:
        raise HTTPException(422, f"provider must be one of {_LLM_PROVIDERS}")
    tid = _caller_tenant(c, tenant_id)
    _require_owner(c, tid)

    from interpreter import vault_secrets

    secret = dict(vault_secrets.get(tid, "llm", sb=_service))
    if secret.pop(f"{provider}_api_key", None) is not None:
        vault_secrets.put(tid, "llm", secret, sb=_service)

    from interpreter import llm as llmmod
    llmmod._tenant_keys_cache.pop(tid, None)

    from interpreter import audit
    audit.record(_service, tenant_id=tid, action="llm_key.removed",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="llm_key", target_id=provider,
                 summary=f"removed the {provider} API key for this workspace")


@app.get("/api/models")
def list_models(tenant_id: str | None = None, c: Caller = Depends(caller)) -> dict:
    """The model roster for the Inspector's model picker, grouped by
    provider, each flagged with whether a real call would actually work
    right now (this tenant's own key, or the platform's)."""
    tid = _caller_tenant(c, tenant_id) if tenant_id else None
    from interpreter import llm as llmmod

    models = [
        {"id": m, "provider": p, "available": llmmod.available(m, tenant_id=tid)}
        for m, p in sorted(llmmod.MODELS.items())
    ]
    return {"models": models, "default_model": llmmod.DEFAULT_MODEL, "fast_model": llmmod.FAST_MODEL}


# ── self-serve Salesforce connection + org introspection (2026-09-03) ──
class SalesforceOrgIn(BaseModel):
    org_label: str = "default"
    tenant_id: str | None = None
    # a raw creds bag mirroring the SF_* env var names -- _build_client
    # self-detects JWT bearer / OAuth username-password / legacy SOAP by
    # which keys are present, same as the env-configured client already does.
    creds: dict[str, str]


@app.get("/api/integrations/salesforce")
def salesforce_orgs(tenant_id: str | None = None, c: Caller = Depends(caller)) -> list[dict]:
    """Every Salesforce org this tenant has connected — never the secret.
    `secret` is already the `redact_org_secret()` view as of 2026-09-04
    (`save_tenant_org` writes it that way, real creds live in Vault) — do
    NOT redact it again here: re-running redact_org_secret on already-safe
    data recomputes `has_credentials` from a dict with no secret keys left
    and silently reports `False` even for a fully connected org."""
    tid = _caller_tenant(c, tenant_id)
    rows = (_service.table("tenant_integrations").select("org_label, secret, updated_at")
            .eq("tenant_id", tid).eq("kind", "salesforce").order("org_label").execute().data or [])
    return [{"org_label": r["org_label"], "updated_at": r["updated_at"], **(r["secret"] or {})}
            for r in rows]


@app.put("/api/integrations/salesforce", status_code=201)
def salesforce_connect(body: SalesforceOrgIn, c: Caller = Depends(caller)) -> dict:
    from interpreter import salesforce as _sf

    tid = _caller_tenant(c, body.tenant_id)
    _require_owner(c, tid)
    rate_limit(c.user_id, "integration", 20)
    if not body.creds.get("SF_USERNAME"):
        raise HTTPException(422, "SF_USERNAME is required")
    result = _sf.test_connection(body.creds)
    if not result["ok"]:
        raise HTTPException(422, f"could not connect: {result['error']}")
    _sf.save_tenant_org(tid, body.org_label, body.creds, sb=_service)

    from interpreter import audit
    audit.record(_service, tenant_id=tid, action="salesforce_org.connected",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="salesforce_org", target_id=body.org_label,
                 summary=f"connected Salesforce org {body.org_label!r}")
    return {"org_label": body.org_label, **_sf.redact_org_secret(body.creds)}


@app.delete("/api/integrations/salesforce/{org_label}", status_code=204)
def salesforce_disconnect(org_label: str, tenant_id: str | None = None,
                          c: Caller = Depends(caller)) -> None:
    from interpreter import salesforce as _sf

    tid = _caller_tenant(c, tenant_id)
    _require_owner(c, tid)
    _sf.delete_tenant_org(tid, org_label, sb=_service)

    from interpreter import audit
    audit.record(_service, tenant_id=tid, action="salesforce_org.disconnected",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="salesforce_org", target_id=org_label,
                 summary=f"disconnected Salesforce org {org_label!r}")


@app.post("/api/integrations/salesforce/test")
def salesforce_test(body: SalesforceOrgIn, c: Caller = Depends(caller)) -> dict:
    """Log in with the posted creds and back out — saves nothing."""
    from interpreter import salesforce as _sf

    tid = _caller_tenant(c, body.tenant_id)
    _require_owner(c, tid)
    rate_limit(c.user_id, "integration", 20)
    return _sf.test_connection(body.creds)


@app.get("/api/integrations/salesforce/{org_label}/schema")
def salesforce_schema(org_label: str, tenant_id: str | None = None,
                      c: Caller = Depends(caller)) -> dict:
    """The connected org's real Case fields (+ picklist values) and Queues
    — what the flow editor's dropdowns/mapping UI are built from, instead
    of hardcoded platform field names."""
    from interpreter import salesforce as _sf

    tid = _caller_tenant(c, tenant_id)
    _require_editor(c, tid)   # editors build flows, not just owners
    rate_limit(c.user_id, "integration", 20)
    return _sf.introspect_org(tid, org_label)


# ── self-serve Salesforce OAuth (2026-09-03) ────────────────────────────
# The "Connect Salesforce" button — one click, no Salesforce-admin setup on
# the customer's side, unlike the JWT path above. Needs a Connected App
# registered once for this platform (SF_OAUTH_CLIENT_ID/SECRET) — degrades
# to "not configured" (503) until then, same as Google before its own
# Connected App existed.
SF_OAUTH_REDIRECT_URI = os.environ.get(
    "SF_OAUTH_REDIRECT_URI", "http://localhost:8000/api/integrations/salesforce/oauth/callback"
)
# nonce -> (expires_at, user_id, tenant_id, org_label, domain) — separate from
# _oauth_state (Google/Slack) since this flow carries two extra fields.
_sf_oauth_state: dict[str, tuple[float, str, str, str, str]] = {}


@app.get("/api/integrations/salesforce/oauth/status")
def salesforce_oauth_status() -> dict:
    from interpreter import salesforce_oauth as _sfo
    return {"configured": _sfo.available()}


@app.get("/api/integrations/salesforce/oauth/authorize")
def salesforce_oauth_authorize(org_label: str = "default", domain: str = "",
                               tenant_id: str | None = None,
                               c: Caller = Depends(caller)) -> dict:
    from interpreter import salesforce_oauth as _sfo

    if not _sfo.available():
        raise HTTPException(503, "Salesforce OAuth is not configured on this server")
    tid = _caller_tenant(c, tenant_id)
    _require_owner(c, tid)
    nonce = secrets.token_urlsafe(24)
    _sf_oauth_state[nonce] = (time.time() + 600, c.user_id, tid, org_label.strip() or "default", domain)
    return {"url": _sfo.authorize_url(SF_OAUTH_REDIRECT_URI, nonce, domain=domain or None)}


@app.get("/api/integrations/salesforce/oauth/callback")
def salesforce_oauth_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    def page(msg: str) -> HTMLResponse:
        return HTMLResponse(
            f"<!doctype html><meta charset=utf-8><p>{msg}</p>"
            "<script>setTimeout(()=>window.close(),1500)</script>"
        )

    if error:
        return page(f"Salesforce authorisation failed: {error}")
    hit = _sf_oauth_state.pop(state, None)
    if not hit or hit[0] < time.time():
        return page("This authorisation link has expired — try again.")
    _, user_id, tid, org_label, domain = hit

    from interpreter import salesforce as _sf
    from interpreter import salesforce_oauth as _sfo

    try:
        tok = _sfo.exchange_code(code, SF_OAUTH_REDIRECT_URI, domain=domain or None)
    except Exception as e:  # noqa: BLE001
        return page(f"Could not complete Salesforce sign-in: {e}")

    _sf.save_tenant_org(tid, org_label, {
        "SF_OAUTH_REFRESH_TOKEN": tok["refresh_token"],
        "SF_OAUTH_INSTANCE_URL": tok["instance_url"],
    }, sb=_service)

    from interpreter import audit
    audit.record(_service, tenant_id=tid, action="salesforce_org.connected",
                 actor_id=user_id, target_type="salesforce_org", target_id=org_label,
                 summary=f"connected Salesforce org {org_label!r} via OAuth")
    return page(f"Salesforce org {org_label!r} connected. You can close this window.")


# ── Multi-provider connectors step 2: connect a Zendesk account ──────────
class ZendeskConnectionIn(BaseModel):
    subdomain: str | None = None
    email: str | None = None
    api_token: str | None = None       # write-only, never returned; Vault-backed
    auto_send_enabled: bool | None = None
    tenant_id: str | None = None


def _zendesk_cfg_from_body(tenant_id: str, body: "ZendeskConnectionIn", existing):
    from interpreter.zendesk import ZendeskConfig

    return ZendeskConfig(
        tenant_id=tenant_id,
        subdomain=(body.subdomain or (existing.subdomain if existing else "")).strip(),
        email=(body.email or (existing.email if existing else "")).strip(),
        status=(existing.status if existing else "inactive"),
        auto_send_enabled=(body.auto_send_enabled if body.auto_send_enabled is not None
                           else (existing.auto_send_enabled if existing else False)),
    )


@app.get("/api/integrations/zendesk")
def zendesk_status(tenant_id: str | None = None, c: Caller = Depends(caller)) -> dict:
    """Connection status for the caller's tenant. Never returns the token."""
    tid = _caller_tenant(c, tenant_id)
    from interpreter.zendesk import load_channel

    ch = load_channel(tid, _service)
    if not ch:
        return {"tenant_id": tid, "configured": False, "status": "none"}
    return {"tenant_id": tid, **ch.public_status()}


@app.put("/api/integrations/zendesk")
def zendesk_configure(body: ZendeskConnectionIn, c: Caller = Depends(caller)) -> dict:
    tid = _caller_tenant(c, body.tenant_id)
    _require_owner(c, tid)
    rate_limit(c.user_id, "integration", 30)
    from interpreter.zendesk import load_channel, save_channel

    existing = load_channel(tid, _service)
    if not (body.subdomain or (existing and existing.subdomain)):
        raise HTTPException(422, "subdomain is required")
    if not (body.email or (existing and existing.email)):
        raise HTTPException(422, "email is required")
    has_token = bool(body.api_token) or bool(existing and existing.api_token)
    if not has_token:
        raise HTTPException(422, "api_token is required")

    cfg = _zendesk_cfg_from_body(tid, body, existing)
    cfg.status = "active"
    save_channel(cfg, _service, api_token=body.api_token)

    from interpreter import audit
    audit.record(_service, tenant_id=tid,
                 action="zendesk_connection.configured" if existing else "zendesk_connection.connected",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="zendesk_connection", target_id=tid,
                 summary=f"{'updated' if existing else 'connected'} the Zendesk account")
    return zendesk_status(tenant_id=tid, c=c)


@app.delete("/api/integrations/zendesk", status_code=204)
def zendesk_disconnect(tenant_id: str | None = None, c: Caller = Depends(caller)) -> None:
    tid = _caller_tenant(c, tenant_id)
    _require_owner(c, tid)
    from interpreter import audit
    from interpreter.zendesk import delete_channel

    delete_channel(tid, _service)
    audit.record(_service, tenant_id=tid, action="zendesk_connection.disconnected",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="zendesk_connection", target_id=tid,
                 summary="disconnected the Zendesk account")


@app.post("/api/integrations/zendesk/test")
def zendesk_test(body: ZendeskConnectionIn, c: Caller = Depends(caller)) -> dict:
    """A lightweight authenticated read (`GET /users/me.json`) — saves
    nothing. Uses the posted token, falling back to the stored one when
    the field is left blank."""
    tid = _caller_tenant(c, body.tenant_id)
    _require_owner(c, tid)
    rate_limit(c.user_id, "integration", 20)
    from interpreter.zendesk import load_channel, test_connection

    existing = load_channel(tid, _service)
    cfg = _zendesk_cfg_from_body(tid, body, existing)
    cfg.api_token = body.api_token or (existing.api_token if existing else "")
    return test_connection(cfg)


# ── Multi-provider connectors step 1: which connector is "the case
# system" for this tenant (tenants.case_connector, migration 084) ────────
class CaseConnectorIn(BaseModel):
    case_connector: str
    tenant_id: str | None = None


@app.get("/api/tenants/case-connector")
def get_case_connector(tenant_id: str | None = None, c: Caller = Depends(caller)) -> dict:
    tid = _caller_tenant(c, tenant_id)
    rows = (_service.table("tenants").select("case_connector")
            .eq("tenant_id", tid).execute().data or [{}])
    return {"tenant_id": tid, "case_connector": rows[0].get("case_connector") or "salesforce"}


@app.put("/api/tenants/case-connector")
def set_case_connector(body: CaseConnectorIn, c: Caller = Depends(caller)) -> dict:
    tid = _caller_tenant(c, body.tenant_id)
    _require_owner(c, tid)
    value = body.case_connector.strip()
    if not value:
        raise HTTPException(422, "case_connector is required")
    # a tenant created before the `tenants` table existed (P7d self-serve
    # workspaces -- e.g. the seeded Globex demo tenant) has no row here at
    # all, so a plain UPDATE silently matches zero rows and "succeeds"
    # without persisting anything (found live-testing this exact
    # endpoint). Upsert instead, using the same "workspace <id prefix>"
    # placeholder name the web UI's own tenantLabel() fallback already
    # uses for a nameless tenant.
    updated = (_service.table("tenants").update({"case_connector": value})
              .eq("tenant_id", tid).execute().data)
    if not updated:
        _service.table("tenants").upsert(
            {"tenant_id": tid, "name": f"workspace {tid[:8]}", "case_connector": value},
        ).execute()

    from interpreter import audit
    audit.record(_service, tenant_id=tid, action="tenant.case_connector_changed",
                 actor_id=c.user_id, actor_email=c.email, target_type="tenant", target_id=tid,
                 summary=f"case system set to {value!r}")
    return {"tenant_id": tid, "case_connector": value}


# ── Per-tenant case-taxonomy config (migration 086) — overrides for the
# module/submodule/region/case-type keyword rules
# interpreter/case_taxonomy.py's map_case_fields/map_case_type use. See
# PROJECT_SCOPE.md "Scoped, not built: per-tenant case-taxonomy config". ──
class CaseTaxonomyIn(BaseModel):
    config: dict[str, Any]
    tenant_id: str | None = None


@app.get("/api/tenants/case-taxonomy")
def get_case_taxonomy(tenant_id: str | None = None, c: Caller = Depends(caller)) -> dict:
    tid = _caller_tenant(c, tenant_id)
    from interpreter import case_taxonomy

    rows = (_service.table("case_taxonomy").select("config, updated_at")
            .eq("tenant_id", tid).execute().data or [])
    return {
        "tenant_id": tid,
        "config": rows[0]["config"] if rows else {},
        "updated_at": rows[0].get("updated_at") if rows else None,
        "defaults": case_taxonomy.DEFAULT_TAXONOMY,
    }


@app.put("/api/tenants/case-taxonomy")
def set_case_taxonomy(body: CaseTaxonomyIn, c: Caller = Depends(caller)) -> dict:
    tid = _caller_tenant(c, body.tenant_id)
    _require_owner(c, tid)
    from interpreter import case_taxonomy

    errs = case_taxonomy.validate_config(body.config)
    if errs:
        raise HTTPException(422, f"invalid case-taxonomy config: {'; '.join(errs)}")
    _service.table("case_taxonomy").upsert(
        {"tenant_id": tid, "config": body.config, "updated_by": c.user_id},
    ).execute()
    case_taxonomy.invalidate(tid)

    from interpreter import audit
    audit.record(_service, tenant_id=tid, action="tenant.case_taxonomy_changed",
                 actor_id=c.user_id, actor_email=c.email, target_type="tenant", target_id=tid,
                 summary="case taxonomy config updated",
                 metadata={"overridden_keys": sorted(body.config.keys())})
    return {"tenant_id": tid, "config": body.config}


@app.delete("/api/tenants/case-taxonomy", status_code=204)
def reset_case_taxonomy(tenant_id: str | None = None, c: Caller = Depends(caller)) -> None:
    tid = _caller_tenant(c, tenant_id)
    _require_owner(c, tid)
    from interpreter import case_taxonomy

    _service.table("case_taxonomy").delete().eq("tenant_id", tid).execute()
    case_taxonomy.invalidate(tid)

    from interpreter import audit
    audit.record(_service, tenant_id=tid, action="tenant.case_taxonomy_reset",
                 actor_id=c.user_id, actor_email=c.email, target_type="tenant", target_id=tid,
                 summary="case taxonomy reset to defaults")


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
def list_rules(team: str | None = None, tenant_id: str | None = None,
               c: Caller = Depends(caller)) -> list[dict]:
    q = c.sb.table("policy_rules").select("*")
    if team:
        q = q.eq("team", team)
    if tenant_id:
        q = q.eq("tenant_id", tenant_id)
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
        created = c.sb.table("policy_rules").insert(row).execute().data[0]
    except Exception as e:  # noqa: BLE001
        raise HTTPException(409, f"could not create rule: {e}")

    from interpreter import audit
    audit.record(_service, tenant_id=tenant_id, action="policy_rule.created",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="policy_rule", target_id=created["rule_id"],
                 summary=f"created rule {body.name!r} ({body.team})")
    return created


@app.patch("/api/rules/{rule_id}")
def update_rule(rule_id: str, body: RulePatch, c: Caller = Depends(caller)) -> dict:
    rate_limit(c.user_id, "rules_write", 60)
    cur = (c.sb.table("policy_rules").select("rule_id, tenant_id, name")
           .eq("rule_id", rule_id).execute().data)
    if not cur:
        raise HTTPException(404, "rule not found or not visible to you")
    _require_editor(c, cur[0]["tenant_id"])
    patch = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    patch["updated_by"] = c.user_id
    updated = c.sb.table("policy_rules").update(patch).eq("rule_id", rule_id).execute().data[0]

    from interpreter import audit
    audit.record(_service, tenant_id=cur[0]["tenant_id"], action="policy_rule.updated",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="policy_rule", target_id=rule_id,
                 summary=f"updated rule {updated.get('name', cur[0].get('name', rule_id))!r}",
                 metadata={"changed_fields": sorted(patch.keys() - {"updated_by"})})
    return updated


@app.delete("/api/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: str, c: Caller = Depends(caller)) -> None:
    rate_limit(c.user_id, "rules_write", 60)
    cur = (c.sb.table("policy_rules").select("tenant_id, name")
           .eq("rule_id", rule_id).execute().data)
    if cur:
        _require_editor(c, cur[0]["tenant_id"])
    c.sb.table("policy_rules").delete().eq("rule_id", rule_id).execute()

    if cur:
        from interpreter import audit
        audit.record(_service, tenant_id=cur[0]["tenant_id"], action="policy_rule.deleted",
                     actor_id=c.user_id, actor_email=c.email,
                     target_type="policy_rule", target_id=rule_id,
                     summary=f"deleted rule {cur[0].get('name', rule_id)!r}")


# --------------------------------------------------------------------------- #
# Intake checklists (migration 101 / interpreter/intake.py) — the per-issue
# investigation spec that drives the `clarify` node's questions.
# --------------------------------------------------------------------------- #
class IntakeChecklistIn(BaseModel):
    label: str
    match: dict[str, Any] = {}
    signals: list[dict[str, Any]] = []
    priority: int = 0
    enabled: bool = True
    tenant_id: str | None = None


class IntakeChecklistPatch(BaseModel):
    label: str | None = None
    match: dict[str, Any] | None = None
    signals: list[dict[str, Any]] | None = None
    priority: int | None = None
    enabled: bool | None = None


def _validate_intake_signals(signals: list[dict[str, Any]] | None) -> None:
    """Reject a checklist whose detect rules won't compile — a bad or
    pathological regex would otherwise hang the `clarify` node at runtime."""
    import re as _re

    for s in signals or []:
        rx = ((s or {}).get("detect") or {}).get("regex")
        if rx is None:
            continue
        if not isinstance(rx, str) or len(rx) > 400:
            raise HTTPException(422, f"signal {s.get('key')!r}: detect.regex must be a "
                                     "string under 400 chars")
        try:
            _re.compile(rx)
        except _re.error as e:
            raise HTTPException(422, f"signal {s.get('key')!r}: invalid regex ({e})")


class IntakePreviewIn(BaseModel):
    subject: str = ""
    body: str = ""
    topic: str = ""
    case_type: str = ""
    module: str = ""
    submodule: str = ""
    tenant_id: str | None = None


@app.get("/api/intake/checklists")
def list_intake_checklists(tenant_id: str | None = None,
                           c: Caller = Depends(caller)) -> list[dict]:
    tid = _caller_tenant(c, tenant_id)
    return (c.sb.table("intake_checklists").select("*")
            .eq("tenant_id", tid).order("priority", desc=True).execute().data or [])


@app.post("/api/intake/checklists", status_code=201)
def create_intake_checklist(body: IntakeChecklistIn, c: Caller = Depends(caller)) -> dict:
    rate_limit(c.user_id, "intake_write", 60)
    tid = _caller_tenant(c, body.tenant_id)
    _require_editor(c, tid)
    _validate_intake_signals(body.signals)
    row = {"tenant_id": tid, "label": body.label, "match": body.match,
           "signals": body.signals, "priority": body.priority, "enabled": body.enabled}
    try:
        created = c.sb.table("intake_checklists").insert(row).execute().data[0]
    except Exception as e:  # noqa: BLE001
        raise HTTPException(409, f"could not create checklist: {e}")

    from interpreter import audit
    audit.record(_service, tenant_id=tid, action="intake_checklist.created",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="intake_checklist", target_id=created["checklist_id"],
                 summary=f"created intake checklist {body.label!r}")
    return created


@app.patch("/api/intake/checklists/{checklist_id}")
def update_intake_checklist(checklist_id: str, body: IntakeChecklistPatch,
                            c: Caller = Depends(caller)) -> dict:
    rate_limit(c.user_id, "intake_write", 60)
    cur = (c.sb.table("intake_checklists").select("checklist_id, tenant_id, label")
           .eq("checklist_id", checklist_id).execute().data)
    if not cur:
        raise HTTPException(404, "checklist not found or not visible to you")
    _require_editor(c, cur[0]["tenant_id"])
    if body.signals is not None:
        _validate_intake_signals(body.signals)
    patch = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    patch["updated_at"] = _now_iso()
    updated = (c.sb.table("intake_checklists").update(patch)
               .eq("checklist_id", checklist_id).execute().data[0])

    from interpreter import audit
    audit.record(_service, tenant_id=cur[0]["tenant_id"], action="intake_checklist.updated",
                 actor_id=c.user_id, actor_email=c.email,
                 target_type="intake_checklist", target_id=checklist_id,
                 summary=f"updated intake checklist {updated.get('label', checklist_id)!r}",
                 metadata={"changed_fields": sorted(patch.keys() - {"updated_at"})})
    return updated


@app.delete("/api/intake/checklists/{checklist_id}", status_code=204)
def delete_intake_checklist(checklist_id: str, c: Caller = Depends(caller)) -> None:
    rate_limit(c.user_id, "intake_write", 60)
    cur = (c.sb.table("intake_checklists").select("tenant_id, label")
           .eq("checklist_id", checklist_id).execute().data)
    if cur:
        _require_editor(c, cur[0]["tenant_id"])
    c.sb.table("intake_checklists").delete().eq("checklist_id", checklist_id).execute()

    if cur:
        from interpreter import audit
        audit.record(_service, tenant_id=cur[0]["tenant_id"], action="intake_checklist.deleted",
                     actor_id=c.user_id, actor_email=c.email,
                     target_type="intake_checklist", target_id=checklist_id,
                     summary=f"deleted intake checklist {cur[0].get('label', checklist_id)!r}")


@app.post("/api/intake/preview")
def preview_intake(body: IntakePreviewIn, c: Caller = Depends(caller)) -> dict:
    """Dry-run the matcher + extractor against a sample case so an author can
    see which checklist fires and the exact questions the bot would ask."""
    tid = _caller_tenant(c, body.tenant_id)
    from interpreter import intake

    state = {
        "tenant_id": tid,
        "case": {"subject": body.subject, "body": body.body},
        "classification": {"topic": body.topic, "case_type": body.case_type,
                           "module": body.module, "submodule": body.submodule},
        "attachments": [],
    }
    cl = intake.checklist_for(state, sb=c.sb)
    if not cl:
        return {"matched": None, "known": {}, "sources": {}, "gaps": [],
                "questions": [], "field_writes": {}}
    ex = intake.extract(cl, state, use_llm=bool((body.subject or body.body).strip()))
    gap = intake.gaps(cl, ex["known"])
    return {
        "matched": cl["label"],
        "known": ex["known"],
        "sources": ex["sources"],
        "gaps": [s["key"] for s in gap],
        "questions": intake.questions_for(gap, 3),
        "field_writes": intake.field_writes(cl, ex["known"]),
    }


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
    from interpreter import vault_secrets

    vault_id = vault_secrets.put(tenant_id, "slack", {
        "bot_token": tok["bot_token"], "team": tok.get("team"),
        "bot_user_id": tok.get("bot_user_id"),
    }, sb=_service)
    row = {"tenant_id": tenant_id, "kind": "slack",
           "secret": {"team": tok.get("team"), "has_credentials": True}}
    if vault_id:
        row["vault_secret_id"] = vault_id
    _service.table("tenant_integrations").upsert(row).execute()
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

    from interpreter import approvals
    res = approvals.decide_action_request(
        _service, ar_id, approve=(decision == "approve"), decided_by=user)
    if res.get("skipped"):
        return PlainTextResponse(res["skipped"] if res["skipped"] != "unknown"
                                 else "unknown request")
    sl, ar = res["slack"], res["ar"]
    try:
        if sl["channel"] and sl["ts"]:
            slackmod.update_message(ar["tenant_id"], sl["channel"], sl["ts"], sl["text"], _service)
    except Exception:  # noqa: BLE001
        pass
    return PlainTextResponse("ok")
