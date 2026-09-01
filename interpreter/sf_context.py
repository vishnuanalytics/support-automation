"""
Phase 25 — the Salesforce picture around a Case: the **organization**
(Account + parent hierarchy), the **people** (the sender Contact + siblings
on the Account, the Account team Users, the owner), a **Lead** if the sender
isn't a known Contact, and **Case history** (open / total + recent).

`identify` resolves *who sent this* (Contact / Account / Lead ids). This node
takes those and loads the fuller context into `state.sf_context`, so the
reasoning + draft can say things like "enterprise account, 3 open Cases,
their CSM is Priya".

    from interpreter.sf_context import load
    ctx = load(state["sender"], want={"account", "contacts", "cases", "team"}, tenant_id=tid)

Best-effort: any missing piece is left out, nothing raises.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("interpreter.sf_context")

ALL_WANT = ("account", "contacts", "leads", "cases", "team")

_OPEN_STATUSES = ("New", "Working", "Escalated", "In Progress", "Waiting on Customer")


def _q(sf, soql: str):
    try:
        return sf.query(soql).get("records", [])
    except Exception as e:  # noqa: BLE001
        log.warning("sf_context query failed: %s | %s", e, soql[:120])
        return []


def _lit(v: str) -> str:
    from interpreter.salesforce import _soql_lit
    return _soql_lit(v)


def load(sender: dict | None, *, want: "set[str] | list[str] | None" = None,
         tenant_id: str | None = None) -> dict[str, Any]:
    from interpreter import salesforce

    sender = sender or {}
    want = set(want or ALL_WANT)
    out: dict[str, Any] = {}
    if not salesforce.available():
        return out
    sf = salesforce.client_for(tenant_id)
    aid = sender.get("account_id")
    cid = sender.get("contact_id")
    lid = sender.get("lead_id")

    # ── organization: Account + one parent hop + child count ──────────
    if aid and "account" in want:
        rows = _q(sf, "SELECT Id, Name, Type, Industry, Rating, NumberOfEmployees, "
                      "OwnerId, Owner.Name, ParentId, Parent.Name, "
                      f"Customer_Type__c, Region__c FROM Account WHERE Id = '{_lit(aid)}'")
        if not rows:                                    # org without those custom fields
            rows = _q(sf, "SELECT Id, Name, Type, Industry, OwnerId, Owner.Name, "
                          f"ParentId, Parent.Name FROM Account WHERE Id = '{_lit(aid)}'")
        if rows:
            a = rows[0]
            out["account"] = {
                "id": a["Id"], "name": a.get("Name"), "type": a.get("Type"),
                "industry": a.get("Industry"), "rating": a.get("Rating"),
                "employees": a.get("NumberOfEmployees"),
                "tier": a.get("Customer_Type__c"), "region": a.get("Region__c"),
                "owner_user": (a.get("Owner") or {}).get("Name"),
                "owner_user_id": a.get("OwnerId"),
                "parent_id": a.get("ParentId"),
                "parent_name": (a.get("Parent") or {}).get("Name"),
            }
            kids = _q(sf, "SELECT COUNT(Id) c FROM Account "
                          f"WHERE ParentId = '{_lit(aid)}'")
            top = a.get("ParentId") or a["Id"]
            out["organization"] = {
                "root_account_id": top,
                "name": (a.get("Parent") or {}).get("Name") or a.get("Name"),
                "child_accounts": (kids[0].get("c") if kids else 0) or 0,
            }

    # ── people: the sender Contact + siblings on the Account ─────────
    if "contacts" in want:
        if cid:
            rows = _q(sf, "SELECT Id, Name, Email, Title, Phone, "
                          f"Contact_Role__c FROM Contact WHERE Id = '{_lit(cid)}'")
            if not rows:
                rows = _q(sf, "SELECT Id, Name, Email, Title, Phone "
                              f"FROM Contact WHERE Id = '{_lit(cid)}'")
            if rows:
                c = rows[0]
                out["contact"] = {"id": c["Id"], "name": c.get("Name"),
                                  "email": c.get("Email"), "title": c.get("Title"),
                                  "phone": c.get("Phone"),
                                  "role": c.get("Contact_Role__c")}
        if aid:
            sibs = _q(sf, "SELECT Name, Email, Title FROM Contact "
                          f"WHERE AccountId = '{_lit(aid)}'"
                          + (f" AND Id != '{_lit(cid)}'" if cid else "")
                          + " ORDER BY LastActivityDate DESC NULLS LAST LIMIT 8")
            out["siblings"] = [{"name": s.get("Name"), "email": s.get("Email"),
                                "title": s.get("Title")} for s in sibs]

    # ── a Lead, when the sender isn't a Contact ─────────────────────
    if lid and "leads" in want:
        rows = _q(sf, "SELECT Id, Name, Company, Email, Status, LeadSource, "
                      f"IsConverted FROM Lead WHERE Id = '{_lit(lid)}'")
        if rows:
            l = rows[0]
            out["lead"] = {"id": l["Id"], "name": l.get("Name"),
                           "company": l.get("Company"), "email": l.get("Email"),
                           "status": l.get("Status"), "source": l.get("LeadSource"),
                           "converted": l.get("IsConverted")}

    # ── Case history on the Account ────────────────────────────────
    if aid and "cases" in want:
        recent = _q(sf, "SELECT CaseNumber, Subject, Status, IsClosed, "
                        f"CreatedDate, ClosedDate FROM Case WHERE AccountId = '{_lit(aid)}' "
                        "ORDER BY CreatedDate DESC LIMIT 6")
        total = _q(sf, f"SELECT COUNT(Id) c FROM Case WHERE AccountId = '{_lit(aid)}'")
        open_ = _q(sf, f"SELECT COUNT(Id) c FROM Case WHERE AccountId = '{_lit(aid)}' "
                       "AND IsClosed = false")
        out["cases"] = {
            "total": (total[0].get("c") if total else 0) or 0,
            "open": (open_[0].get("c") if open_ else 0) or 0,
            "recent": [{"number": r.get("CaseNumber"), "subject": r.get("Subject"),
                        "status": r.get("Status"), "closed": r.get("IsClosed"),
                        "created": r.get("CreatedDate")} for r in recent],
        }

    # ── who covers this account (Account team + owner) ─────────────
    if aid and "team" in want:
        team = _q(sf, "SELECT UserId, User.Name, TeamMemberRole FROM AccountTeamMember "
                      f"WHERE AccountId = '{_lit(aid)}'")
        members = [{"user_id": t.get("UserId"), "name": (t.get("User") or {}).get("Name"),
                    "role": t.get("TeamMemberRole")} for t in team]
        if not members and out.get("account", {}).get("owner_user_id"):
            members = [{"user_id": out["account"]["owner_user_id"],
                        "name": out["account"].get("owner_user"), "role": "Account Owner"}]
        out["account_team"] = members

    return out
