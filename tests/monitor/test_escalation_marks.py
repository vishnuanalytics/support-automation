"""The escalation log: escalate, team responded, reopen. Only Case Monitor's own table."""

from datetime import datetime, timezone

import pytest

from interpreter.monitor import service

T, CASE, USER = "t1", "500A", "u1"
NOW = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)


class Table:
    def __init__(self, rows):
        self.rows, self.filters, self.op, self.payload = rows, {}, "select", None

    def select(self, *_):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def upsert(self, row):
        self.op, self.payload = "upsert", dict(row)
        return self

    def delete(self):
        self.op = "delete"
        return self

    def _match(self, r):
        return all(r.get(k) == v for k, v in self.filters.items())

    def execute(self):
        if self.op == "upsert":
            self.rows[:] = [r for r in self.rows
                            if (r["tenant_id"], r["case_id"]) != (self.payload["tenant_id"], self.payload["case_id"])]
            self.rows.append(self.payload)
            data = [self.payload]
        elif self.op == "delete":
            self.rows[:] = [r for r in self.rows if not self._match(r)]
            data = []
        else:
            data = [dict(r) for r in self.rows if self._match(r)]
        return type("R", (), {"data": data})()


class Sb:
    def __init__(self):
        self.rows = []

    def table(self, name):
        assert name == "monitor_escalation_marks"
        return Table(self.rows)


def test_escalate_then_responded_then_reopen():
    sb = Sb()
    row = service.set_escalation_mark(sb, T, CASE, USER, "escalate", "  Zomato KAM by email ", now=NOW)
    assert row["manual"] is True and row["escalated_at"] == NOW.isoformat()
    assert row["note"] == "Zomato KAM by email" and row["responded_at"] is None

    row = service.set_escalation_mark(sb, T, CASE, USER, "responded", now=LATER)
    assert row["responded_at"] == LATER.isoformat() and row["escalated_at"] == NOW.isoformat()
    assert row["note"] == "Zomato KAM by email"  # a later action without a note keeps it

    row = service.set_escalation_mark(sb, T, CASE, USER, "reopen", now=LATER)
    assert row["responded_at"] is None and len(sb.rows) == 1


def test_responded_on_a_salesforce_listed_case_is_not_manual():
    sb = Sb()
    row = service.set_escalation_mark(sb, T, CASE, USER, "responded", now=NOW)
    assert row["manual"] is False and row["escalated_at"] is None and row["responded_at"] == NOW.isoformat()


def test_escalating_again_clears_responded():
    sb = Sb()
    service.set_escalation_mark(sb, T, CASE, USER, "responded", now=NOW)
    row = service.set_escalation_mark(sb, T, CASE, USER, "escalate", now=LATER)
    assert row["manual"] is True and row["responded_at"] is None


def test_clear_and_list_are_tenant_scoped():
    sb = Sb()
    service.set_escalation_mark(sb, T, CASE, USER, "escalate", now=NOW)
    service.set_escalation_mark(sb, "t2", CASE, USER, "escalate", now=NOW)
    service.clear_escalation_mark(sb, T, CASE)
    assert service.escalation_marks(sb, T) == []
    assert len(service.escalation_marks(sb, "t2")) == 1


def test_unknown_action_is_refused():
    with pytest.raises(ValueError):
        service.set_escalation_mark(Sb(), T, CASE, USER, "close")
