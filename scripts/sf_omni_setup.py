"""
Phase 27b — Omni-Channel setup for the case-control-plane, done entirely through
the Salesforce API (no Setup clicks, no Flow, no Apex).

**No routing trigger is needed.** Once a queue has a QueueRoutingConfig,
Salesforce auto-creates the PendingServiceRouting the moment a Case's OwnerId
is set to that queue — which the pipeline's `ask_human` / `handover` nodes
already do via `salesforce.assign_case(queue=…)`. Verified live: a re-assign
to `Team_CSM` produced a ready PSR with zero extra code.

    python scripts/sf_omni_setup.py --dry-run
    python scripts/sf_omni_setup.py                 # everything
    python scripts/sf_omni_setup.py --only routing

Stages (each idempotent — re-running skips what already exists):
  channel    ServiceChannel `Support_Case` (RelatedEntity = Case, TabBased)
  statuses   ServicePresenceStatus Available_Cases / Busy / Away
             + ServiceChannelStatus link (Available_Cases <-> Support_Case)
  routing    QueueRoutingConfig RC_Standard (LeastActive, pri 2, cap 1, 90s)
             + RC_Priority (pri 1); attached to the Team_* / reason queues
  presence   PresenceUserConfig PC_Support_Agent (capacity 3, decline on)
  assign     add the running user to PC_Support_Agent

Two things are NOT API-settable — do them once in Setup:
  * grant the agent profile/permset access to the presence statuses
    (Permission Set > … > Service Presence Statuses)
  * an agent must open the console, add the Omni-Channel utility, and set
    presence to "Available — Cases" for work to actually push (otherwise
    the PSR just sits ready-and-pending).

Needs SF creds in .env and a user with "Customize Application" + Omni-Channel
admin (System Administrator has both).
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from interpreter.salesforce import _client, available  # noqa: E402

CHANNEL_DEV = "Support_Case"
STATUSES = [("Available_Cases", "Available — Cases"), ("Busy", "Busy"), ("Away", "Away")]
RC_STANDARD = "RC_Standard"
RC_PRIORITY = "RC_Priority"
PRESENCE_CFG = "PC_Support_Agent"

STANDARD_QUEUES = ["Team_Support", "Support_Tier2", "Team_CSM", "Team_Sales", "Team_Offboarding"]
PRIORITY_QUEUES = ["Enterprise_Support", "Billing_Escalations"]


def _first(sf, soql):
    r = sf.query(soql).get("records", [])
    return r[0] if r else None


# ── stages ──────────────────────────────────────────────────────────────
def stage_channel(sf, dry: bool) -> str | None:
    row = _first(sf, f"SELECT Id FROM ServiceChannel WHERE DeveloperName = '{CHANNEL_DEV}'")
    if row:
        print(f"  ServiceChannel {CHANNEL_DEV}: exists ({row['Id']})")
        return row["Id"]
    if dry:
        print(f"  ServiceChannel {CHANNEL_DEV}: WOULD create (RelatedEntity=Case)")
        return None
    res = sf.ServiceChannel.create({
        "DeveloperName": CHANNEL_DEV, "MasterLabel": "Support Case",
        "RelatedEntity": "Case", "CapacityModel": "TabBased",   # fixed weight per item
    })
    print(f"  ServiceChannel {CHANNEL_DEV}: created ({res['id']})")
    return res["id"]


def stage_statuses(sf, dry: bool, channel_id: str | None) -> None:
    have = {r["DeveloperName"]: r["Id"] for r in
            sf.query("SELECT Id, DeveloperName FROM ServicePresenceStatus").get("records", [])}
    for dev, label in STATUSES:
        if dev in have:
            print(f"  ServicePresenceStatus {dev}: exists")
            continue
        if dry:
            print(f"  ServicePresenceStatus {dev}: WOULD create")
            continue
        res = sf.ServicePresenceStatus.create({"DeveloperName": dev, "MasterLabel": label})
        have[dev] = res["id"]
        print(f"  ServicePresenceStatus {dev}: created")
    # link Available_Cases -> the channel so an agent in that status receives Cases
    if not channel_id or dry:
        print("  ServiceChannelStatus link: (needs channel id / live run)")
        return
    avail = have.get("Available_Cases")
    if not avail:
        return
    linked = sf.query(
        f"SELECT Id FROM ServiceChannelStatus WHERE ServiceChannelId = '{channel_id}' "
        f"AND ServicePresenceStatusId = '{avail}'"
    ).get("records", [])
    if linked:
        print("  ServiceChannelStatus link: exists")
    else:
        sf.ServiceChannelStatus.create(
            {"ServiceChannelId": channel_id, "ServicePresenceStatusId": avail})
        print("  ServiceChannelStatus link: created (Available_Cases <-> Support_Case)")


def _ensure_rc(sf, dry: bool, dev: str, *, priority: int) -> str | None:
    row = _first(sf, f"SELECT Id FROM QueueRoutingConfig WHERE DeveloperName = '{dev}'")
    if row:
        print(f"  QueueRoutingConfig {dev}: exists ({row['Id']})")
        return row["Id"]
    if dry:
        print(f"  QueueRoutingConfig {dev}: WOULD create (LeastActive, pri {priority}, cap 1, 90s)")
        return None
    res = sf.QueueRoutingConfig.create({
        "DeveloperName": dev, "MasterLabel": dev.replace("_", " "),
        "RoutingModel": "LeastActive", "RoutingPriority": priority,
        "CapacityWeight": 1, "PushTimeout": 90,
    })
    print(f"  QueueRoutingConfig {dev}: created ({res['id']})")
    return res["id"]


def stage_routing(sf, dry: bool) -> None:
    std = _ensure_rc(sf, dry, RC_STANDARD, priority=2)
    pri = _ensure_rc(sf, dry, RC_PRIORITY, priority=1)
    pairs = ([(q, std, RC_STANDARD) for q in STANDARD_QUEUES]
             + [(q, pri, RC_PRIORITY) for q in PRIORITY_QUEUES])
    for qname, rc_id, rc_name in pairs:
        q = _first(sf, f"SELECT Id, DeveloperName, QueueRoutingConfigId FROM Group "
                       f"WHERE Type = 'Queue' AND DeveloperName = '{qname}'")
        if not q:
            print(f"  queue {qname}: MISSING (run sf_support_setup.py --only queues)")
            continue
        if q.get("QueueRoutingConfigId"):
            print(f"  queue {qname}: routing config already set")
            continue
        if dry or not rc_id:
            print(f"  queue {qname}: WOULD attach {rc_name}")
            continue
        sf.Group.update(q["Id"], {"QueueRoutingConfigId": rc_id})
        print(f"  queue {qname}: attached {rc_name}")


def stage_presence(sf, dry: bool) -> str | None:
    row = _first(sf, f"SELECT Id FROM PresenceUserConfig WHERE DeveloperName = '{PRESENCE_CFG}'")
    if row:
        print(f"  PresenceUserConfig {PRESENCE_CFG}: exists ({row['Id']})")
        return row["Id"]
    if dry:
        print(f"  PresenceUserConfig {PRESENCE_CFG}: WOULD create (capacity 3)")
        return None
    res = sf.PresenceUserConfig.create({
        "DeveloperName": PRESENCE_CFG, "MasterLabel": "Support Agent",
        "Capacity": 3, "OptionsIsDeclineEnabled": True,
    })
    print(f"  PresenceUserConfig {PRESENCE_CFG}: created ({res['id']})")
    return res["id"]


def stage_assign(sf, dry: bool, cfg_id: str | None) -> None:
    me = _first(sf, f"SELECT Id, Name FROM User WHERE Username = '{os.environ['SF_USERNAME']}'")
    if not cfg_id or dry:
        print(f"  assign {me['Name'] if me else '?'} -> {PRESENCE_CFG}: (needs cfg id / live run)")
        return
    existing = sf.query(
        f"SELECT Id FROM PresenceUserConfigUser WHERE PresenceUserConfigId = '{cfg_id}' "
        f"AND UserId = '{me['Id']}'"
    ).get("records", [])
    if existing:
        print(f"  assign {me['Name']} -> {PRESENCE_CFG}: already assigned")
        return
    try:
        sf.PresenceUserConfigUser.create({"PresenceUserConfigId": cfg_id, "UserId": me["Id"]})
        print(f"  assign {me['Name']} -> {PRESENCE_CFG}: assigned")
    except Exception as e:  # noqa: BLE001
        print(f"  assign: failed — {e}")
    print("  NB: also grant the agent's profile/permset access to the presence statuses "
          "(Setup > Permission Sets > … > Service Presence Statuses) — not API-settable.")


def main() -> int:
    stages = ["channel", "statuses", "routing", "presence", "assign"]
    ap = argparse.ArgumentParser(prog="scripts.sf_omni_setup")
    ap.add_argument("--only", choices=stages, action="append")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not available():
        sys.exit("no SF creds in .env")
    sf = _client()
    run = args.only or stages
    ch_id = cfg_id = None
    if "channel" in run:
        print("\n== channel ==")
        ch_id = stage_channel(sf, args.dry_run)
    if "statuses" in run:
        print("\n== statuses ==")
        ch_id = ch_id or (_first(sf, f"SELECT Id FROM ServiceChannel WHERE DeveloperName='{CHANNEL_DEV}'") or {}).get("Id")
        stage_statuses(sf, args.dry_run, ch_id)
    if "routing" in run:
        print("\n== routing ==")
        stage_routing(sf, args.dry_run)
    if "presence" in run:
        print("\n== presence ==")
        cfg_id = stage_presence(sf, args.dry_run)
    if "assign" in run:
        print("\n== assign ==")
        cfg_id = cfg_id or (_first(sf, f"SELECT Id FROM PresenceUserConfig WHERE DeveloperName='{PRESENCE_CFG}'") or {}).get("Id")
        stage_assign(sf, args.dry_run, cfg_id)
    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
