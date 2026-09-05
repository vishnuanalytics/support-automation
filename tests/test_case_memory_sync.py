"""
Regression coverage for a real bug found while investigating why Neo4j's
DUPLICATE_OF edges never fire (0 exist despite 232 SIMILAR_TO edges, per
docs/MULTI_SYSTEM_ARCHITECTURE.md): `account_id` was silently never making
it onto `case_memory` rows produced by either sync source, so `same_account`
(case_memory_sync._sync_rows) was always False and DUPLICATE_OF's
same-account gate (interpreter/case_memory.py's `_MERGE_CYPHER`) never
passed. Offline: Salesforce is mocked, no live creds needed.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

for _k in ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN", "SF_CONSUMER_KEY",
           "SF_CONSUMER_SECRET", "SF_PRIVATE_KEY", "SF_PRIVATE_KEY_FILE"):
    os.environ.pop(_k, None)

from interpreter import salesforce
from ingestion import case_memory_sync as cms

# real Salesforce Ids are always 15 or 18 chars -- _enrich_from_sf filters on
# that, so fake ids in these tests must match the shape.
_CASE_1 = "500XX0000000001AAA"[:18]
_CASE_2 = "500XX0000000002BBB"[:18]
_CASE_3 = "500XX0000000003CCC"[:18]


class _FakeSF:
    def __init__(self, records):
        self._records = records

    def query(self, soql):
        return {"records": self._records}


@pytest.fixture(autouse=True)
def _no_real_sf(monkeypatch):
    monkeypatch.setattr(salesforce, "_client_obj", None, raising=False)


def test_enrich_backfills_account_id_even_when_case_type_already_set(monkeypatch):
    # Root cause: the old `want` filter only checked `case_type`, so a row
    # that already had it (e.g. from sf_writeback) was treated as fully
    # enriched and never re-queried for AccountId.
    monkeypatch.setattr(salesforce, "available", lambda: True)
    monkeypatch.setattr(salesforce, "client_for",
                        lambda *a, **k: _FakeSF([{"Id": _CASE_1, "CaseNumber": "00099",
                                                  "Type": "Bug", "Module__c": "Billing",
                                                  "Region__c": "US", "AccountId": "001XX9",
                                                  "Account": {"Tier__c": "gold"},
                                                  "IsClosed": True, "ClosedDate": "2026-08-01"}]))
    rows = [{"case_sf_id": _CASE_1, "case_type": "Bug", "account_id": None}]
    cms._enrich_from_sf(rows)
    assert rows[0]["account_id"] == "001XX9"


def test_enrich_skips_the_sf_round_trip_when_nothing_is_missing(monkeypatch):
    monkeypatch.setattr(salesforce, "available", lambda: True)
    calls = []
    monkeypatch.setattr(salesforce, "client_for",
                        lambda *a, **k: calls.append(1) or _FakeSF([]))
    rows = [{"case_sf_id": _CASE_1, "case_type": "Bug", "account_id": "001XX9"}]
    cms._enrich_from_sf(rows)
    assert not calls   # already fully enriched -- no SF call needed


def test_enrich_still_backfills_a_row_missing_case_type(monkeypatch):
    monkeypatch.setattr(salesforce, "available", lambda: True)
    monkeypatch.setattr(salesforce, "client_for",
                        lambda *a, **k: _FakeSF([{"Id": _CASE_2, "CaseNumber": "00100",
                                                  "Type": "How-to", "Module__c": "Reports",
                                                  "Region__c": "EU", "AccountId": "001XX8",
                                                  "Account": {}, "IsClosed": True,
                                                  "ClosedDate": "2026-08-02"}]))
    rows = [{"case_sf_id": _CASE_2, "case_type": None, "account_id": None}]
    cms._enrich_from_sf(rows)
    assert rows[0]["case_type"] == "How-to"
    assert rows[0]["account_id"] == "001XX8"


class _FakeSFMulti:
    """`_from_salesforce` reuses one client for both the Case query and a
    per-case EmailMessage query -- dispatch on which one is being asked."""
    def query(self, soql):
        if "FROM EmailMessage" in soql:
            return {"records": [{"TextBody": "Here's the fix."}]}
        return {"records": [{"Id": _CASE_3, "CaseNumber": "00101", "Subject": "Help",
                             "Description": "desc", "Type": "Bug", "Module__c": "Billing",
                             "Region__c": "US", "AccountId": "001XX7",
                             "Account": {"Tier__c": "silver"}, "ClosedDate": "2026-08-03"}]}


def test_from_salesforce_includes_account_id_directly(monkeypatch):
    monkeypatch.setattr(salesforce, "available", lambda: True)
    monkeypatch.setattr(salesforce, "_soql_lit", lambda s: s)
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _FakeSFMulti())
    out = cms._from_salesforce("2026-08-01", 10)
    assert len(out) == 1
    assert out[0]["account_id"] == "001XX7"
    assert out[0]["source"] == "salesforce"
