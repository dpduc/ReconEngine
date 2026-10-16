"""Expert handoff through the public seam: resolution actions, the audit log, metrics and close status."""

import json

import pytest

from recon.pipeline import (
    CONFIRM,
    MARK_REFUND,
    OVERRIDE,
    REOPEN,
    WRITE_OFF,
    AuditLog,
    close_status,
    handoff_metrics,
    unresolved_queue,
    run,
    run_key,
)

from test_pipeline import BANK, SHIP, acknowledged, write_sources

# Queue: ORD..3 (MISSING_PAYMENT, 70.00), P9 (ORPHAN_PAYMENT, 70.00), ORD..2 (AMOUNT_MISMATCH, 50.00), T9 (ORPHAN_SHIPMENT).
ORD2, ORD3 = "ORD2024-0000002", "ORD2024-0000003"


@pytest.fixture
def outcome(tmp_path):
    bank = BANK + "P9,TRANSFER NO REFERENCE,70.00,0,70.00,2024-01-20\n"
    ship = SHIP + "T9,ORD2024-0009999,12.0,6.0,0\n"
    return run(tmp_path, acknowledged(tmp_path, write_sources(tmp_path, bank=bank, ship=ship)))


def test_each_action_is_logged_with_the_engines_decision_and_survives_a_reload(tmp_path, outcome):
    path = tmp_path / "audit" / "log.jsonl"
    log = AuditLog(path)
    log.record(outcome, "oms", ORD3, OVERRIDE, actor="ana", note="paid by P9", matched_ref="P9",
               at="2024-02-01T09:00:00+00:00")
    log.record(outcome, "oms", ORD2, CONFIRM, actor="ana", at="2024-02-01T10:00:00+00:00")
    log.record(outcome, "bank", "P9", MARK_REFUND, actor="bo", note="refund to sender",
               at="2024-02-02T09:00:00+00:00")
    log.record(outcome, "logistics", "T9", WRITE_OFF, actor="bo", note="lost parcel",
               at="2024-02-02T11:00:00+00:00")

    reloaded = AuditLog(path).entries()
    actions = [e for e in reloaded if e.event != "queued"]
    assert [(e.event, e.record_ref, e.actor) for e in actions] == [
        (OVERRIDE, ORD3, "ana"), (CONFIRM, ORD2, "ana"), (MARK_REFUND, "P9", "bo"), (WRITE_OFF, "T9", "bo")]
    override = actions[0]
    assert (override.engine_decision, override.engine_reason_codes, override.matched_ref, override.note) == \
        ("EXCEPTION", "MISSING_PAYMENT", "P9", "paid by P9")
    assert override.amount_at_risk == 70.0 and override.engine_step and override.run_key == run_key(outcome)
    # Append-only: one JSON line per entry, the queue registration first.
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5 and json.loads(lines[0])["event"] == "queued"


def test_resolved_items_leave_the_open_queue_and_reopen_brings_them_back(tmp_path, outcome):
    log = AuditLog(tmp_path / "log.jsonl")
    assert len(unresolved_queue(outcome, log)) == 4
    log.record(outcome, "oms", ORD2, CONFIRM, actor="ana")
    assert ORD2 not in set(unresolved_queue(outcome, AuditLog(tmp_path / "log.jsonl"))["record_ref"])

    with pytest.raises(ValueError, match="already resolved"):
        log.record(outcome, "oms", ORD2, WRITE_OFF, actor="ana", note="changed my mind")
    log.record(outcome, "oms", ORD2, REOPEN, actor="ana", note="amount was not confirmed")
    assert ORD2 in set(unresolved_queue(outcome, log)["record_ref"])


def test_actions_are_validated(tmp_path, outcome):
    log = AuditLog(tmp_path / "log.jsonl")
    with pytest.raises(ValueError, match="who"):
        log.record(outcome, "oms", ORD2, CONFIRM, actor=" ")
    with pytest.raises(ValueError, match="note"):
        log.record(outcome, "oms", ORD2, WRITE_OFF, actor="ana")
    with pytest.raises(ValueError, match="matched to"):
        log.record(outcome, "oms", ORD3, OVERRIDE, actor="ana", note="paid")
    with pytest.raises(ValueError, match="not in the run's exception queue"):
        log.record(outcome, "oms", "ORD2024-0000001", CONFIRM, actor="ana")
    with pytest.raises(ValueError, match="nothing to reopen"):
        log.record(outcome, "oms", ORD2, REOPEN, actor="ana", note="x")
    assert not (tmp_path / "log.jsonl").exists()


def test_handoff_metrics_are_computed_from_the_log_per_reason_code(tmp_path, outcome):
    log = AuditLog(tmp_path / "log.jsonl")
    log.register_queue(outcome, at="2024-02-01T08:00:00+00:00")
    log.record(outcome, "oms", ORD3, OVERRIDE, actor="ana", note="paid by P9", matched_ref="P9",
               at="2024-02-01T10:00:00+00:00")  # 2 h
    log.record(outcome, "oms", ORD2, CONFIRM, actor="ana", at="2024-02-01T12:00:00+00:00")
    log.record(outcome, "oms", ORD2, REOPEN, actor="ana", note="recheck", at="2024-02-01T13:00:00+00:00")
    log.record(outcome, "oms", ORD2, WRITE_OFF, actor="ana", note="small", at="2024-02-02T14:00:00+00:00")  # 30 h

    m = handoff_metrics(outcome, AuditLog(tmp_path / "log.jsonl"), now="2024-02-03T08:00:00+00:00")
    assert (m["queue_items"], m["resolved"], m["unresolved"]) == (4, 2, 2)
    assert m["override_rate"] == 0.5
    assert m["first_touch_rate"] == 0.5  # ORD2 needed three actions
    assert m["throughput_per_day"] == {"2024-02-01": 1, "2024-02-02": 1}
    assert m["actions"] == {CONFIRM: 1, OVERRIDE: 1, WRITE_OFF: 1, MARK_REFUND: 0, REOPEN: 1}
    per = m["per_reason_code"]
    assert per["MISSING_PAYMENT"] == {"queued": 1, "resolved": 1, "unresolved": 0, "overrides": 1, "override_rate": 1.0,
                                      "median_hours_to_resolution": 2.0, "unresolved_age_hours": None}
    assert per["AMOUNT_MISMATCH"]["override_rate"] == 0.0
    assert per["AMOUNT_MISMATCH"]["median_hours_to_resolution"] == 30.0
    assert per["ORPHAN_PAYMENT"]["unresolved"] == 1 and per["ORPHAN_PAYMENT"]["unresolved_age_hours"] == 48.0
    assert m["evidence_completeness"] == 1.0  # every item: source rows, explanation, recommended action


def test_close_status_lists_blockers_and_the_watchlist_to_revisit_first(tmp_path, outcome):
    log = AuditLog(tmp_path / "log.jsonl")
    status = close_status(outcome, log)
    assert not status["can_close"] and status["unresolved"] == 4
    assert list(status["blockers"]["reason_code"])[:2] == ["MISSING_PAYMENT", "ORPHAN_PAYMENT"]
    assert "revisit_first" in status

    for record_type, ref in [("oms", ORD3), ("bank", "P9"), ("oms", ORD2), ("logistics", "T9")]:
        log.record(outcome, record_type, ref, CONFIRM, actor="ana")
    status = close_status(outcome, log)
    assert status["can_close"] and status["blockers"].empty and status["amount_at_risk"] == 0.0


def test_a_new_run_of_the_same_inputs_keeps_its_queue(tmp_path, outcome):
    config = acknowledged(tmp_path, write_sources(
        tmp_path, bank=BANK + "P9,TRANSFER NO REFERENCE,70.00,0,70.00,2024-01-20\n",
        ship=SHIP + "T9,ORD2024-0009999,12.0,6.0,0\n"))
    again = run(tmp_path, config)
    assert run_key(again) == run_key(outcome)
    log = AuditLog(tmp_path / "log.jsonl")
    log.record(outcome, "oms", ORD2, CONFIRM, actor="ana")
    assert ORD2 not in set(unresolved_queue(again, log)["record_ref"])
