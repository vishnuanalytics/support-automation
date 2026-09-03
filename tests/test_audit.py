"""Phase 28 -- the platform activity/audit log."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import audit


class _Q:
    def __init__(self, sink):
        self._sink = sink

    def insert(self, row):
        self._sink.append(row)
        return self

    def execute(self):
        return type("R", (), {"data": self._sink[-1:]})


class _SB:
    def __init__(self):
        self.inserted: list[dict] = []

    def table(self, name):
        assert name == "audit_log"
        return _Q(self.inserted)


class _BrokenSB:
    def table(self, name):
        raise RuntimeError("db is down")


def test_record_inserts_the_row_shape():
    sb = _SB()
    audit.record(
        sb, tenant_id="t1", action="flow.published",
        actor_id="u1", actor_email="a@b.com",
        target_type="flow", target_id="f1", summary="published X v3",
        metadata={"version": 3},
    )
    assert len(sb.inserted) == 1
    row = sb.inserted[0]
    assert row == {
        "tenant_id": "t1", "action": "flow.published",
        "actor_id": "u1", "actor_email": "a@b.com",
        "target_type": "flow", "target_id": "f1",
        "summary": "published X v3", "metadata": {"version": 3},
    }


def test_record_defaults_metadata_to_empty_dict_and_stringifies_target_id():
    sb = _SB()
    audit.record(sb, tenant_id="t1", action="member.removed", target_id=42)
    row = sb.inserted[0]
    assert row["metadata"] == {}
    assert row["target_id"] == "42"
    assert row["actor_id"] is None
    assert row["actor_email"] is None


def test_record_never_raises_on_a_broken_client():
    # must not propagate -- an audit-log failure can never break the real
    # mutation it's recording.
    audit.record(_BrokenSB(), tenant_id="t1", action="flow.deleted")
