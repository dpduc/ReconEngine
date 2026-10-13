"""Orphans, exception queue, per-reason-code evaluation and the export bundle, via run()."""

import io
import json
from dataclasses import replace

import pandas as pd

from recon.pipeline import run

from test_pipeline import BANK, SHIP, acknowledged, write_sources


def test_unlinked_bank_and_logistics_records_become_orphans(tmp_path):
    bank = BANK + "P9,TRANSFER NO REFERENCE,42.00,0,42.00,2024-01-05\n"
    ship = SHIP + "T9,ORD2024-0009999,0,6.0,0\n"
    outcome = run(tmp_path, acknowledged(tmp_path, write_sources(tmp_path, bank=bank, ship=ship)))

    orphans = outcome.orphans.set_index("reason_codes")
    assert sorted(orphans.index) == ["ORPHAN_PAYMENT", "ORPHAN_SHIPMENT"]
    assert orphans.loc["ORPHAN_PAYMENT", "txn_ref"] == "P9"
    assert orphans.loc["ORPHAN_SHIPMENT", "tracking_code"] == "T9"
    assert (outcome.orphans["decision"] == "EXCEPTION").all()
    assert outcome.orphans["input_row_hashes"].str.len().gt(0).all()
    # OMS-driven units are unaffected by the reverse pass.
    assert len(outcome.results) == 3


def test_records_of_orders_decided_before_linking_are_attached_not_orphaned(tmp_path):
    from test_funnel_order import order, payment, run_feeds, shipment

    # Order 1 has no amount, order 2 has a status the policy doesn't cover: both are decided
    # before linking, yet their payment and shipment name them and are not orphans.
    outcome = run_feeds(tmp_path, oms=[order(1, amount=""), order(2, status="canceled"), order(3)],
                        bank=[payment(1), payment(2), payment(3)],
                        ship=[shipment(1), shipment(2), shipment(3)])

    assert outcome.orphans.empty
    results = outcome.results.set_index("order_id")
    assert results.loc["ORD2024-0000001", "reason_codes"] == "MISSING_SOURCE_EVIDENCE"
    assert results.loc["ORD2024-0000002", "reason_codes"] == "ORDER_STATUS_REQUIRES_POLICY"
    assert results.loc["ORD2024-0000001", "txn_ref"] == "P1"
    assert results.loc["ORD2024-0000002", "tracking_code"] == "T2"
    # Each unit traces to its OMS, bank and logistics rows.
    assert results["input_row_hashes"].str.split("|").map(len).eq(3).all()


def test_pass_3_candidate_payments_are_not_also_orphans(tmp_path):
    from test_linking import OMS_TWO, SHIP_TWO
    from test_linking import write_sources as write_linking_sources

    bank = ("P1,PAYMENT ORD2024-0000001,100.00,0,100.00,2024-01-02\n"
            "P2,TRANSFER RECEIVED,42.50,0,42.50,2024-01-11\n"
            "P3,TRANSFER RECEIVED,42.50,0,42.50,2024-01-11\n")
    config = write_linking_sources(tmp_path, oms=OMS_TWO, bank=bank, ship=SHIP_TWO)
    outcome = run(tmp_path, acknowledged(tmp_path, config))

    assert outcome.results.set_index("order_id").loc["ORD2024-0000002", "decision"] == "REVIEW"
    assert outcome.orphans.empty


def test_exception_queue_ranks_by_amount_at_risk_then_age(tmp_path):
    # Unit 2: amount mismatch on a 50.00 order; unit 3: missing payment on 70.00 (ordered 01-03).
    # P9: 70.00 orphan payment valued 01-20 (outside any linking date window) ties with unit 3 but is younger. T9: orphan, 12.00 COD.
    bank = BANK + "P9,TRANSFER NO REFERENCE,70.00,0,70.00,2024-01-20\n"
    ship = SHIP + "T9,ORD2024-0009999,12.0,6.0,0\n"
    outcome = run(tmp_path, acknowledged(tmp_path, write_sources(tmp_path, bank=bank, ship=ship)))

    queue = outcome.exception_queue
    assert list(queue["record_ref"]) == ["ORD2024-0000003", "P9", "ORD2024-0000002", "T9"]
    assert list(queue["amount_at_risk"]) == [70.0, 70.0, 50.0, 12.0]
    assert queue["age_days"].iloc[0] > queue["age_days"].iloc[1]
    assert (queue["decision"] == "EXCEPTION").all()


def test_exception_queue_includes_review_units_with_their_decision(tmp_path):
    from test_linking import OMS_TWO, SHIP_TWO
    from test_linking import write_sources as write_linking_sources

    # Order 2 (42.50) has two equally good candidates: REVIEW. P9 (60.00) is an orphan.
    bank = ("P1,PAYMENT ORD2024-0000001,100.00,0,100.00,2024-01-02\n"
            "P2,TRANSFER RECEIVED,42.50,0,42.50,2024-01-11\n"
            "P3,TRANSFER RECEIVED,42.50,0,42.50,2024-01-11\n"
            "P9,TRANSFER NO REFERENCE,60.00,0,60.00,2024-01-30\n")
    config = write_linking_sources(tmp_path, oms=OMS_TWO, bank=bank, ship=SHIP_TWO)
    queue = run(tmp_path, acknowledged(tmp_path, config)).exception_queue

    assert list(queue["record_ref"]) == ["P9", "ORD2024-0000002"]
    assert list(queue["decision"]) == ["EXCEPTION", "REVIEW"]
    assert list(queue["amount_at_risk"]) == [60.0, 42.5]


TRUTH = """order_id,scenario,sub_kind,expected_outcome,expected_exception
ORD2024-0000001,MATCHED,MATCHED,PASS_1,
ORD2024-0000002,AMOUNT_ERROR,OVERPAID,EXCEPTION,AMOUNT_MISMATCH
ORD2024-0000003,AMOUNT_ERROR,UNDERPAID,EXCEPTION,AMOUNT_MISMATCH
"""


def test_exception_queue_is_empty_but_filterable_when_every_unit_is_accepted(tmp_path):
    from test_linking import OMS_TWO, SHIP_TWO
    from test_linking import write_sources as write_linking_sources

    bank = ("P1,PAYMENT ORD2024-0000001,100.00,0,100.00,2024-01-02\n"
            "P2,PAYMENT ORD2024-0000002,42.50,0,42.50,2024-01-11\n")
    outcome = run(tmp_path, write_linking_sources(tmp_path, oms=OMS_TWO, bank=bank, ship=SHIP_TWO))
    assert (outcome.results["decision"] == "AUTO_ACCEPT").all()

    queue = outcome.exception_queue
    assert queue.empty
    assert {"record_type", "record_ref", "reason_codes", "deciding_step"} <= set(queue.columns)


def test_precision_and_recall_per_reason_code_against_truth(tmp_path):
    # Predicted: order 2 AMOUNT_MISMATCH (right), order 3 MISSING_PAYMENT (truth says AMOUNT_MISMATCH).
    (tmp_path / "truth.csv").write_text(TRUTH, encoding="utf-8")
    config = replace(write_sources(tmp_path), ground_truth="truth.csv")
    per_code = run(tmp_path, acknowledged(tmp_path, config)).manifest["metrics"]["per_reason_code"]

    assert per_code["AMOUNT_MISMATCH"] == {"predicted": 1, "actual": 2, "true_positives": 1,
                                           "precision": 1.0, "recall": 0.5}
    assert per_code["MISSING_PAYMENT"] == {"predicted": 1, "actual": 0, "true_positives": 0,
                                           "precision": 0.0, "recall": None}
    assert "THREE_WAY_TIE_OUT" not in per_code


def test_export_bundle_holds_every_artifact_and_reads_back(tmp_path):
    bank = BANK + "P9,TRANSFER NO REFERENCE,70.00,0,70.00,2024-01-20\n"
    outcome = run(tmp_path, acknowledged(tmp_path, write_sources(tmp_path, bank=bank)))
    bundle = outcome.bundle()

    assert set(bundle) == {"decisions.csv", "triage_queue.csv", "exception_queue.csv",
                           "orphans.csv", "funnel.csv", "excluded.csv", "manifest.json"}
    read = lambda name: pd.read_csv(io.StringIO(bundle[name]))
    assert len(read("decisions.csv")) == 3
    assert list(read("orphans.csv")["txn_ref"]) == ["P9"]
    assert list(read("exception_queue.csv")["record_ref"]) == ["ORD2024-0000003", "P9", "ORD2024-0000002"]
    assert read("funnel.csv")["decided"].sum() == 3
    assert "decision" in read("triage_queue.csv").columns
    manifest = json.loads(bundle["manifest.json"])
    assert manifest["metrics"]["orphans"] == {"ORPHAN_PAYMENT": 1, "ORPHAN_SHIPMENT": 0}


def test_each_decision_is_explained_in_plain_language_with_deltas_and_confidence(tmp_path):
    bank = BANK + "P9,TRANSFER NO REFERENCE,42.00,0,42.00,2024-01-05\n"
    outcome = run(tmp_path, acknowledged(tmp_path, write_sources(tmp_path, bank=bank)))
    explained = outcome.results.set_index("order_id")["explanation"]

    assert explained["ORD2024-0000001"].startswith("Order, bank credit and shipment tie out.")
    assert explained["ORD2024-0000002"].startswith("The bank credit is 5.00 more than expected")
    assert explained["ORD2024-0000003"].startswith("No bank credit found for this order.")
    assert explained["ORD2024-0000003"].endswith("Confidence 0.15.")
    assert outcome.orphans["explanation"].iloc[0].startswith("This bank credit matches no order.")
