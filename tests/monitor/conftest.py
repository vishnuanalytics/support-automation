from datetime import datetime, timezone

import pytest

from interpreter.monitor.config import DEFAULT_CONFIG

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    return {**DEFAULT_CONFIG,
            "third_party_label": "Aggregator", "customer_label": "Merchant",
            "third_party_overrides": {"PartnerA": 12, "PartnerB": 6},
            "fields": {**DEFAULT_CONFIG["fields"], "link": "External_Link__c",
                       "last_reply_from": "Last_Reply_From__c",
                       "target_at": "Target_Date__c"}}


@pytest.fixture
def now():
    return NOW
