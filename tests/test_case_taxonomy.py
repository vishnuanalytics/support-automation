"""
Offline unit tests for interpreter/case_taxonomy.py — the per-tenant
override of the module/submodule/region/case-type keyword rules
map_case_fields/map_case_type/normalize_case_type use (migration 086).
No DB, no network — a fake `sb` stands in for Supabase.

Run:  pytest tests/test_case_taxonomy.py
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import case_taxonomy as ct

TENANT = "11111111-1111-1111-1111-111111111111"


class _FakeTable:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def select(self, *_a, **_kw):
        return self

    def eq(self, *_a, **_kw):
        return self

    def execute(self):
        class R:
            data = self._rows

        return R()


class _FakeSb:
    def __init__(self, config: dict | None):
        self._config = config

    def table(self, name):
        assert name == "case_taxonomy"
        rows = [{"config": self._config}] if self._config is not None else []
        return _FakeTable(rows)


@pytest.fixture(autouse=True)
def _clear_cache():
    ct._cache.clear()
    yield
    ct._cache.clear()


# ── load() / fallback / merge ───────────────────────────────────────────
def test_load_with_no_tenant_is_the_pure_default():
    assert ct.load(None) is ct.DEFAULT_TAXONOMY


def test_load_with_no_row_falls_back_to_default():
    assert ct.load(TENANT, sb=_FakeSb(None)) == ct.DEFAULT_TAXONOMY


def test_load_merges_tenant_override_over_default():
    override = {"region_by_country": {"wakanda": "AFRICA"}}
    merged = ct.load(TENANT, sb=_FakeSb(override))
    # overridden key replaced wholesale
    assert merged["region_by_country"] == {"wakanda": "AFRICA"}
    # every other key still the default
    assert merged["module_rules"] == ct.DEFAULT_TAXONOMY["module_rules"]
    assert merged["case_type_rules"] == ct.DEFAULT_TAXONOMY["case_type_rules"]


def test_load_is_cached_until_invalidated():
    sb = _FakeSb({"region_by_country": {"foo": "BAR"}})
    first = ct.load(TENANT, sb=sb)
    assert first["region_by_country"] == {"foo": "BAR"}
    # change the backing store -- cached result should still win
    sb._config = {"region_by_country": {"foo": "BAZ"}}
    assert ct.load(TENANT, sb=sb)["region_by_country"] == {"foo": "BAR"}
    ct.invalidate(TENANT)
    assert ct.load(TENANT, sb=sb)["region_by_country"] == {"foo": "BAZ"}


def test_empty_override_is_a_pure_default():
    assert ct.load(TENANT, sb=_FakeSb({})) == ct.DEFAULT_TAXONOMY


# ── map_case_fields / map_case_type / normalize_case_type: default parity ──
def test_map_case_fields_default_behavior_unchanged():
    m = ct.map_case_fields("refund-for-duplicate-charge", "United Kingdom")
    assert m["Module__c"] == "Billing & Plans"
    assert m["SubModule__c"] == "Refunds"
    assert m["Region__c"] == "EMEA"
    assert m["Topic__c"] == "refund-for-duplicate-charge"


def test_map_case_fields_empty_input():
    assert ct.map_case_fields("", None) == {}


def test_map_case_type_default_behavior_unchanged():
    assert ct.map_case_type("refund-request") == "Billing"
    assert ct.map_case_type("something totally unrecognized") == "Question"
    assert ct.map_case_type("") == ""


def test_normalize_case_type_default_behavior_unchanged():
    assert ct.normalize_case_type("problem / bug") == "Problem / Bug"
    assert ct.normalize_case_type("BILLING") == "Billing"
    assert ct.normalize_case_type(None) == ""


# ── tenant override actually changes matching behavior ─────────────────
def test_tenant_override_changes_module_mapping():
    override = {
        "module_rules": [{"keywords": ["spaceship"], "module": "Fleet"}],
    }
    sb = _FakeSb(override)
    m = ct.map_case_fields("spaceship-wont-launch", None, tenant_id=TENANT, sb=sb)
    assert m["Module__c"] == "Fleet"
    # the tenant's override replaced module_rules wholesale -- the default
    # "billing" keyword no longer matches for this tenant
    m2 = ct.map_case_fields("billing-question", None, tenant_id=TENANT, sb=sb)
    assert m2.get("Module__c") == "Other"


def test_tenant_override_changes_case_type_mapping():
    override = {"case_type_rules": [{"keywords": ["spaceship"], "case_type": "Fleet Issue"}]}
    sb = _FakeSb(override)
    assert ct.map_case_type("spaceship-broken", tenant_id=TENANT, sb=sb) == "Fleet Issue"


def test_tenant_override_reaches_normalize_case_type_valid_values():
    override = {"case_type_rules": [{"keywords": ["spaceship"], "case_type": "Fleet Issue"}]}
    sb = _FakeSb(override)
    assert ct.normalize_case_type("fleet issue", tenant_id=TENANT, sb=sb) == "Fleet Issue"


# ── valid_values() ───────────────────────────────────────────────────────
def test_valid_values_default_matches_known_picklist_shape():
    vv = ct.valid_values()
    assert "Billing & Plans" in vv["modules"]
    assert vv["modules"][-1] == "Other"
    assert set(vv["submodule_by_module"]["Billing & Plans"]) == {
        "Refunds", "Invoices", "Plan Change", "Charges",
    }
    assert set(vv["regions"]) == {"NA", "EMEA", "APAC", "LATAM", "Other"}
    assert "Billing" in vv["case_types"]
    assert vv["case_types"][-2:] == ["Question", "Other"]


def test_valid_values_reflects_tenant_override():
    override = {"module_rules": [{"keywords": ["spaceship"], "module": "Fleet"}]}
    vv = ct.valid_values(TENANT, sb=_FakeSb(override))
    assert vv["modules"] == ["Fleet", "Other"]


# ── validate_config() ────────────────────────────────────────────────────
def test_validate_config_empty_is_valid():
    assert ct.validate_config({}) == []


def test_validate_config_rejects_non_dict():
    assert ct.validate_config([1, 2, 3]) != []


def test_validate_config_rejects_unknown_key():
    errs = ct.validate_config({"bogus_key": []})
    assert any("unknown key" in e for e in errs)


def test_validate_config_accepts_well_formed_rules():
    cfg = {
        "module_rules": [{"keywords": ["a", "b"], "module": "X"}],
        "submodule_rules": {"X": [{"keywords": ["c"], "submodule": "Y"}]},
        "region_by_country": {"narnia": "OTHERWORLD"},
        "case_type_rules": [{"keywords": ["d"], "case_type": "Z"}],
    }
    assert ct.validate_config(cfg) == []


def test_validate_config_rejects_malformed_module_rules():
    errs = ct.validate_config({"module_rules": [{"keywords": "not-a-list", "module": "X"}]})
    assert any("module_rules" in e for e in errs)


def test_validate_config_rejects_malformed_region_by_country():
    errs = ct.validate_config({"region_by_country": {"narnia": 123}})
    assert any("region_by_country" in e for e in errs)


# ── salesforce.py still re-exports the same functions ───────────────────
def test_salesforce_module_reexports_case_taxonomy_functions():
    from interpreter import salesforce

    assert salesforce.map_case_fields is ct.map_case_fields
    assert salesforce.map_case_type is ct.map_case_type
    assert salesforce.normalize_case_type is ct.normalize_case_type
