"""
Pure decision logic: one decoded CDC change event -> the `run_flow` jobs it
should produce. No I/O, no Salesforce, no Supabase -- unit-testable in
isolation (`tests/test_sf_pubsub_plan.py`).

A `ChangeEvent` payload (Avro-decoded to a dict) looks like:

    {
      "ChangeEventHeader": {
        "entityName": "Case",
        "recordIds": ["500..."],
        "changeType": "CREATE" | "UPDATE" | "DELETE" | "UNDELETE"
                      | "GAP_CREATE" | "GAP_UPDATE" | "GAP_OVERFLOW" | ...,
        "changedFields": ["Case.OwnerId", ...],   # UPDATE only; may be empty
        ...
      },
      "Subject": ..., "Status": ..., "OwnerId": ..., "Incoming": ...,
      # UPDATE: only changed fields are populated, the rest are null
    }

We deliberately do NOT decode the `changedFields` bitmap format -- for an
UPDATE the changed scalar fields are populated in the payload, so a
non-null `OwnerId` on a `Case` UPDATE is a reliable "the queue changed"
signal, and `"...OwnerId"` appearing in `changedFields` is a second path.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunSpec:
    """A `run_flow` job to enqueue for one Salesforce Case."""

    case_id: str
    dedupe_key: str        # one live job per key (jobs.uq_jobs_dedupe)
    idempotency_key: str   # one recorded run per (flow_id, key)
    trigger: str           # why: case_created | inbound_email | case_owner_changed


_REAL_CHANGE = {"CREATE", "UPDATE", "UNDELETE"}


def _owner_changed(payload: dict) -> bool:
    hdr = payload.get("ChangeEventHeader") or {}
    changed = hdr.get("changedFields") or []
    if any(str(f).split(".")[-1] == "OwnerId" for f in changed):
        return True
    # UPDATE payloads carry only the changed fields; a populated OwnerId
    # means it was one of them.
    return bool(payload.get("OwnerId"))


def plan_events(payload: dict, replay_hex: str) -> list[RunSpec]:
    """Return the RunSpec(s) for one decoded change event. Empty list = ignore."""
    hdr = payload.get("ChangeEventHeader") or {}
    entity = hdr.get("entityName")
    change_type = hdr.get("changeType") or ""
    record_ids = [r for r in (hdr.get("recordIds") or []) if r]
    if change_type not in _REAL_CHANGE or not record_ids:
        return []

    specs: list[RunSpec] = []

    if entity == "Case":
        for rid in record_ids:
            if change_type == "CREATE":
                # Same keys the Phase 20i HTTP hook uses, so running both
                # push paths during a migration never double-processes a
                # new Case.
                specs.append(RunSpec(rid, f"sfcase:{rid}", rid, "case_created"))
            elif change_type == "UPDATE" and _owner_changed(payload):
                key = f"sfowner:{rid}:{replay_hex}"
                specs.append(RunSpec(rid, key, key, "case_owner_changed"))

    elif entity == "EmailMessage":
        # A customer's reply lands as a new inbound EmailMessage on the Case.
        # Outbound (our own replies) have Incoming=false -> ignored.
        if change_type == "CREATE" and payload.get("Incoming") is True:
            parent = payload.get("ParentId")
            mid = record_ids[0]
            if parent and str(parent).startswith("500"):   # ParentId is a Case
                specs.append(RunSpec(parent, f"sfemail:{mid}", f"email:{mid}", "inbound_email"))

    return specs
