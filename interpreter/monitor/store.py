"""Case Monitor persistence, behind one small interface."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .verdict import parse_dt

LOCK_EXPIRY = timedelta(minutes=20)
AI_FEED_DAYS = 14
# Terminal reasoning_sessions states, per migration 087.
_AI_DONE_STATES = ("sent", "abandoned")
_CHUNK = 200


class Store(Protocol):
    def start_sync(self, tenant_id: str, trigger: str) -> str | None: ...
    def finish_sync(self, sync_id: str, status: str, counts: dict, errors: list) -> None: ...
    def ai_feed(self, tenant_id: str) -> dict[str, dict]: ...
    def open_cases(self, tenant_id: str) -> dict[str, dict]: ...
    def save_cases(self, tenant_id: str, rows: list[dict], now: datetime) -> None: ...
    def save_evidence(self, tenant_id: str, evidence: list[dict], now: datetime) -> None: ...
    def close_cases(self, tenant_id: str, ids: list[str]) -> None: ...
    def add_events(self, tenant_id: str, sync_id: str, events: list[dict]) -> None: ...
    def last_resolved_at(self, tenant_id: str) -> datetime | None: ...
    def save_resolved(self, tenant_id: str, rows: list[dict]) -> None: ...


def _ai_state(run: dict) -> str | None:
    if run.get("outcome") == "need_info":
        return "awaiting_customer"
    if run.get("outcome") in ("ask_human", "handover") and \
            run.get("human_action") in ("pending", "no_reply"):
        return "handed_off"
    return None


class SupabaseStore:
    def __init__(self, sb: Any):
        self.sb = sb

    def start_sync(self, tenant_id: str, trigger: str) -> str | None:
        expired = (datetime.now(timezone.utc) - LOCK_EXPIRY).isoformat()
        (self.sb.table("monitor_syncs")
         .update({"status": "failed", "errors": [{"error": "sync did not finish"}]})
         .eq("tenant_id", tenant_id).eq("status", "running").lt("started_at", expired).execute())
        try:
            res = self.sb.table("monitor_syncs").insert(
                {"tenant_id": tenant_id, "trigger": trigger}).execute()
        except Exception as e:  # noqa: BLE001
            if "23505" in str(e) or "duplicate key" in str(e):
                return None
            raise
        return res.data[0]["id"]

    def finish_sync(self, sync_id: str, status: str, counts: dict, errors: list) -> None:
        self.sb.table("monitor_syncs").update({
            "status": status, "counts": counts, "errors": errors[:200],
            "finished_at": datetime.now(timezone.utc).isoformat()}).eq("id", sync_id).execute()

    def ai_feed(self, tenant_id: str) -> dict[str, dict]:
        """Newest AI run per case, plus any open reasoning session."""
        since = (datetime.now(timezone.utc) - timedelta(days=AI_FEED_DAYS)).isoformat()
        runs = (self.sb.table("runs")
                .select("run_id, case_id, subject, outcome, human_action, created_at")
                .eq("tenant_id", tenant_id).gte("created_at", since)
                .in_("outcome", ["ask_human", "handover", "need_info"])
                .order("created_at", desc=True).limit(1000).execute().data or [])
        out: dict[str, dict] = {}
        seen: set[str] = set()
        for r in runs:
            cid = r.get("case_id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            state = _ai_state(r)
            if state:
                out[cid] = {"state": state, "outcome": r.get("outcome"), "run_id": r["run_id"],
                            "subject": r.get("subject"), "at": r.get("created_at"),
                            "url": f"/?view=runs&run={r['run_id']}"}
        sessions = (self.sb.table("reasoning_sessions")
                    .select("case_id, run_id, state, created_at").eq("tenant_id", tenant_id)
                    .gte("created_at", since)
                    .not_.in_("state", list(_AI_DONE_STATES)).execute().data or [])
        for s in sessions:
            out[s["case_id"]] = {"state": "working", "outcome": s["state"],
                                 "run_id": s.get("run_id"), "at": s.get("created_at"),
                                 "url": f"/?view=runs&run={s.get('run_id')}"}
        return out

    def open_cases(self, tenant_id: str) -> dict[str, dict]:
        rows = (self.sb.table("monitor_cases")
                .select("case_id, case_data, verdict, timeline, timeline_at")
                .eq("tenant_id", tenant_id).eq("is_open", True).neq("source", "demo")
                .execute().data or [])
        return {r["case_id"]: {"case": r["case_data"], "verdict": r["verdict"],
                               "timeline": r["timeline"]} for r in rows}

    def save_cases(self, tenant_id: str, rows: list[dict], now: datetime) -> None:
        stamp = now.isoformat()
        payload = []
        for r in rows:
            row = {"tenant_id": tenant_id, "case_id": r["case"]["id"],
                   "source": r["case"].get("source") or "salesforce", "is_open": True,
                   "case_data": r["case"], "verdict": r["verdict"], "synced_at": stamp}
            payload.append(row)
        for start in range(0, len(payload), _CHUNK):
            self.sb.table("monitor_cases").upsert(payload[start:start + _CHUNK]).execute()
        for r in rows:
            if r.get("timeline") is not None:
                self.save_timeline(tenant_id, r["case"]["id"], r["timeline"], now)

    def save_timeline(self, tenant_id: str, case_id: str, timeline: dict, now: datetime) -> None:
        (self.sb.table("monitor_cases")
         .update({"timeline": timeline, "timeline_at": now.isoformat()})
         .eq("tenant_id", tenant_id).eq("case_id", case_id).execute())

    def save_evidence(self, tenant_id: str, evidence: list[dict], now: datetime) -> None:
        payload = [{"tenant_id": tenant_id, "url": e["url"], "data": e,
                    "fetched_at": now.isoformat()} for e in evidence]
        for start in range(0, len(payload), _CHUNK):
            self.sb.table("monitor_evidence").upsert(payload[start:start + _CHUNK]).execute()

    def close_cases(self, tenant_id: str, ids: list[str]) -> None:
        for start in range(0, len(ids), _CHUNK):
            (self.sb.table("monitor_cases").update({"is_open": False})
             .eq("tenant_id", tenant_id).in_("case_id", ids[start:start + _CHUNK]).execute())

    def add_events(self, tenant_id: str, sync_id: str, events: list[dict]) -> None:
        if events:
            self.sb.table("monitor_events").insert(
                [{"tenant_id": tenant_id, "sync_id": sync_id, **e} for e in events]).execute()

    def last_resolved_at(self, tenant_id: str) -> datetime | None:
        rows = (self.sb.table("monitor_resolved").select("closed_at").eq("tenant_id", tenant_id)
                .not_.like("case_id", "demo-%").not_.is_("closed_at", "null")
                .order("closed_at", desc=True).limit(1).execute().data or [])
        return parse_dt(rows[0]["closed_at"]) if rows else None

    def save_resolved(self, tenant_id: str, rows: list[dict]) -> None:
        payload = [{"tenant_id": tenant_id, "case_id": r["id"],
                    **{k: r.get(k) for k in ("case_number", "subject", "module", "sub_module",
                                             "third_parties", "opened_at", "closed_at",
                                             "resolution_note", "url")}} for r in rows]
        for start in range(0, len(payload), _CHUNK):
            self.sb.table("monitor_resolved").upsert(payload[start:start + _CHUNK]).execute()
