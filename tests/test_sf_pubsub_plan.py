"""Phase 20l — the CDC change-event -> run_flow planner (pure, offline)."""

from ingestion.sf_pubsub.plan import RunSpec, plan_events


def _case(change_type, record_ids, **fields):
    return {
        "ChangeEventHeader": {
            "entityName": "Case", "changeType": change_type, "recordIds": record_ids,
            "changedFields": fields.pop("_changed", []),
        },
        **fields,
    }


def _email(change_type, mid, **fields):
    return {
        "ChangeEventHeader": {
            "entityName": "EmailMessage", "changeType": change_type, "recordIds": [mid],
            "changedFields": fields.pop("_changed", []),
        },
        **fields,
    }


RID = "5003t00000ABCDEadAAF"
PARENT = "5003t00000PARENTaaAAF"
REPLAY = "0a1b2c"


def test_case_create_enqueues_with_the_hook_dedupe_key():
    specs = plan_events(_case("CREATE", [RID], Subject="Help"), REPLAY)
    assert specs == [RunSpec(RID, f"sfcase:{RID}", RID, "case_created")]


def test_case_owner_change_via_populated_field():
    specs = plan_events(_case("UPDATE", [RID], OwnerId="00G3t000000QUEUEaAB"), REPLAY)
    assert len(specs) == 1
    s = specs[0]
    assert s.case_id == RID and s.trigger == "case_owner_changed"
    assert s.dedupe_key == f"sfowner:{RID}:{REPLAY}" and s.idempotency_key == s.dedupe_key


def test_case_owner_change_via_changedfields_list():
    specs = plan_events(_case("UPDATE", [RID], _changed=["Case.OwnerId"]), REPLAY)
    assert [s.trigger for s in specs] == ["case_owner_changed"]


def test_case_update_without_owner_is_ignored():
    assert plan_events(_case("UPDATE", [RID], Status="Escalated"), REPLAY) == []


def test_send_bot_draft_button_arms_a_dedicated_trigger():
    specs = plan_events(_case("UPDATE", [RID], Bot_Send_Draft__c=True), REPLAY)
    assert len(specs) == 1
    s = specs[0]
    assert s.case_id == RID and s.trigger == "bot_send_draft"
    assert s.dedupe_key == f"sfsenddraft:{RID}:{REPLAY}" and s.idempotency_key == s.dedupe_key


def test_send_bot_draft_false_or_absent_is_ignored():
    assert plan_events(_case("UPDATE", [RID], Bot_Send_Draft__c=False), REPLAY) == []
    assert plan_events(_case("UPDATE", [RID], Status="New"), REPLAY) == []


def test_send_bot_draft_ignores_the_workers_own_clear():
    ev = _case("UPDATE", [RID], Bot_Send_Draft__c=True)
    ev["ChangeEventHeader"]["commitUser"] = "005BOT"
    assert plan_events(ev, REPLAY, bot_user_id="005BOT") == []


def test_inbound_email_enqueues_for_the_parent_case():
    specs = plan_events(_email("CREATE", "02s3t00000EMAILaaAAF", Incoming=True, ParentId=PARENT), REPLAY)
    assert specs == [RunSpec(PARENT, "sfemail:02s3t00000EMAILaaAAF", "email:02s3t00000EMAILaaAAF", "inbound_email")]


def test_outbound_email_is_ignored():
    assert plan_events(_email("CREATE", "02s000", Incoming=False, ParentId=PARENT), REPLAY) == []


def test_inbound_email_without_a_case_parent_is_ignored():
    # ParentId that isn't a Case (e.g. a Lead) -> skip
    assert plan_events(_email("CREATE", "02s000", Incoming=True, ParentId="00Q3t00000LEADaaAAF"), REPLAY) == []
    assert plan_events(_email("CREATE", "02s000", Incoming=True), REPLAY) == []


def test_delete_and_gap_events_are_ignored():
    assert plan_events(_case("DELETE", [RID]), REPLAY) == []
    assert plan_events(_case("GAP_UPDATE", [RID], OwnerId="00G000"), REPLAY) == []


def test_no_record_ids_is_ignored():
    assert plan_events(_case("CREATE", []), REPLAY) == []


def test_subscriber_module_imports():
    # generated gRPC stubs + fastavro wiring load
    from ingestion.sf_pubsub.subscriber import DEFAULT_TOPICS, PubSubSubscriber

    assert "/data/CaseChangeEvent" in DEFAULT_TOPICS
    assert PubSubSubscriber is not None


def test_cli_is_a_noop_without_sf_creds(monkeypatch):
    import ingestion.sf_cdc_watch as w

    monkeypatch.setattr(w.salesforce, "available", lambda: False)
    assert w.main([]) == 0
