"""interpreter/case_import.py — parse a Salesforce bulk export / CSV into
case_memory row dicts. Offline; no network."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import case_import

_HDR = ('<?xml version="1.0" encoding="UTF-8"?>'
        '<queryResult xmlns="http://www.force.com/2009/06/asyncapi/dataload">')


def _email(pid, incoming, date, subject, body):
    return (f"<records><type>EmailMessage</type><ParentId>{pid}</ParentId>"
            f"<Incoming>{incoming}</Incoming><MessageDate>{date}</MessageDate>"
            f"<Subject>{subject}</Subject><TextBody>{body}</TextBody></records>")


def _case(cid, closed, number, subject):
    return (f"<records><type>Case</type><Case_Id_18char__c>{cid}</Case_Id_18char__c>"
            f"<IsClosed>{closed}</IsClosed><CaseNumber>{number}</CaseNumber>"
            f"<ClosedDate>2026-08-01T10:00:00.000Z</ClosedDate>"
            f"<Type>Problem</Type><Module__c>Menu</Module__c><Subject>{subject}</Subject></records>")


PID = "500fv00000ABCDE"
EMAIL_XML = (_HDR
             + _email(PID, "true", "2026-08-01T08:00:00Z", "Menu not publishing",
                      "Hi Team, our menu will not publish to Swiggy. Please help.")
             + _email(PID, "false", "2026-08-01T09:00:00Z", "Re: Menu not publishing",
                      "Hi, the publish failed because an item had no price. Add a price "
                      "to 'Veg Roll' in Business Manager and publish again. That resolves it.")
             + _email(PID, "false", "2026-08-01T12:00:00Z", "Re: Menu not publishing",
                      "Thank you for confirming. We are marking this support case as resolved.")
             + "</queryResult>").encode()

CASE_XML = (_HDR + _case(PID + "AA", "true", "00099001", "Menu not publishing")
            + "</queryResult>").encode()


def test_sniff():
    assert case_import.sniff(EMAIL_XML) == "sf_email"
    assert case_import.sniff(CASE_XML) == "sf_case"
    assert case_import.sniff(b"subject,problem,resolution\na,b,c\n") == "csv"


def test_bulk_pairs_problem_and_resolution_skipping_boilerplate():
    rows, stats = case_import.parse_salesforce_bulk(EMAIL_XML, CASE_XML, tenant_id="T1")
    assert stats["kept"] == 1 and stats["closed"] == 1
    r = rows[0]
    assert r["tenant_id"] == "T1"
    assert r["case_number"] == "00099001"
    assert r["case_type"] == "Problem" and r["module"] == "Menu"
    assert "will not publish" in r["body_summary"]
    assert "Add a price to 'Veg Roll'" in r["resolution_text"]
    assert "marking this support case as resolved" not in r["resolution_text"]


def test_bulk_without_case_file_still_works():
    rows, stats = case_import.parse_salesforce_bulk(EMAIL_XML, None, tenant_id="T1")
    assert stats["kept"] == 1
    assert rows[0]["case_sf_id"].startswith("import:")   # no Case id available
    assert rows[0]["case_type"] is None


def test_bulk_drops_thread_with_no_real_answer():
    xml = (_HDR
           + _email(PID, "true", "2026-08-01T08:00:00Z", "Question", "How do I add a logo?")
           + _email(PID, "false", "2026-08-01T09:00:00Z", "Re: Question",
                    "Thanks for reaching out, we are marking this case as resolved.")
           + "</queryResult>").encode()
    rows, stats = case_import.parse_salesforce_bulk(xml, None, tenant_id="T1")
    assert rows == [] and stats["dropped_no_resolution"] == 1


def test_flat_csv():
    csv = (b"Subject,Problem,Resolution,Resolved_At,Case_Number\n"
           b"Prices stale,Swiggy shows old price,"
           b"Re-publish the outlet after saving the price; a stale price means no publish ran.,"
           b"2026-08-01,00042\n"
           b"Too short,x,nope,2026-08-01,00043\n")
    rows, stats = case_import.parse_flat_csv(csv, tenant_id="T2")
    assert stats["kept"] == 1 and stats["dropped_no_resolution"] == 1
    assert rows[0]["case_number"] == "00042"
    assert rows[0]["tenant_id"] == "T2"
    assert "Re-publish the outlet" in rows[0]["resolution_text"]
