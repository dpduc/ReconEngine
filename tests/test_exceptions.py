"""Orphans, exception queue, per-reason-code evaluation and the export bundle, via run()."""

import io
import json
from dataclasses import replace

import pandas as pd

from recon.pipeline import run

from test_pipeline import BANK, SHIP, write_sources


def test_unlinked_bank_and_logistics_records_become_orphans(tmp_path):
    bank = BANK + "P9,TRANSFER NO REFERENCE,42.00,0,42.00,2024-01-05\n"
    ship = SHIP + "T9,ORD2024-0009999,0,6.0,0\n"
    outcome = run(tmp_path, write_sources(tmp_path, bank=bank, ship=ship))

    orphans = outcome.orphans.set_index("reason_codes")
    assert sorted(orphans.index) == ["ORPHAN_PAYMENT", "ORPHAN_SHIPMENT"]
    assert orphans.loc["ORPHAN_PAYMENT", "txn_ref"] == "P9"
    assert orphans.loc["ORPHAN_SHIPMENT", "tracking_code"] == "T9"
    assert (outcome.orphans["decision"] == "EXCEPTION").all()
    assert outcome.orphans["input_row_hashes"].str.len().gt(0).all()
    # OMS-driven units are unaffected by the reverse pass.
    assert len(outcome.results) == 3


def test_exception_queue_ranks_by_amount_at_risk_then_age(tmp_path):
    # Unit 2: amount mismatch on a 50.00 order; unit 3: missing payment on 70.00 (ordered 01-03).
    # P9: 70.00 orphan payment valued 01-05 ties with unit 3 but is younger. T9: orphan, 12.00 COD.
    bank = BANK + "P9,TRANSFER NO REFERENCE,70.00,0,70.00,2024-01-05\n"
    ship = SHIP + "T9,ORD2024-0009999,12.0,6.0,0\n"
    outcome = run(tmp_path, write_sources(tmp_path, bank=bank, ship=ship))

    queue = outcome.exception_queue
    assert list(queue["record_ref"]) == ["ORD2024-0000003", "P9", "ORD2024-0000002", "T9"]
    assert list(queue["amount_at_risk"]) == [70.0, 70.0, 50.0, 12.0]
    assert queue["age_days"].iloc[0] > queue["age_days"].iloc[1]
    assert (queue["decision"] == "EXCEPTION").all()


TRUTH = """order_id,scenario,sub_kind,expected_outcome,expected_exception
ORD2024-0000001,MATCHED,MATCHED,PASS_1,
ORD2024-0000002,AMOUNT_ERROR,OVERPAID,EXCEPTION,AMOUNT_MISMATCH
ORD2024-0000003,AMOUNT_ERROR,UNDERPAID,EXCEPTION,AMOUNT_MISMATCH
"""


def test_precision_and_recall_per_reason_code_against_truth(tmp_path):
    # Predicted: order 2 AMOUNT_MISMATCH (right), order 3 MISSING_PAYMENT (truth says AMOUNT_MISMATCH).
    (tmp_path / "truth.csv").write_text(TRUTH, encoding="utf-8")
    config = replace(write_sources(tmp_path), ground_truth="truth.csv")
    per_code = run(tmp_path, config).manifest["metrics"]["per_reason_code"]

    assert per_code["AMOUNT_MISMATCH"] == {"predicted": 1, "actual": 2, "true_positives": 1,
                                           "precision": 1.0, "recall": 0.5}
    assert per_code["MISSING_PAYMENT"] == {"predicted": 1, "actual": 0, "true_positives": 0,
                                           "precision": 0.0, "recall": None}
    assert "THREE_WAY_TIE_OUT" not in per_code


def test_export_bundle_holds_every_artifact_and_reads_back(tmp_path):
    bank = BANK + "P9,TRANSFER NO REFERENCE,70.00,0,70.00,2024-01-05\n"
    outcome = run(tmp_path, write_sources(tmp_path, bank=bank))
    bundle = outcome.bundle()

    assert set(bundle) == {"decisions.csv", "triage_queue.csv", "exception_queue.csv",
                           "orphans.csv", "funnel.csv", "manifest.json"}
    read = lambda name: pd.read_csv(io.StringIO(bundle[name]))
    assert len(read("decisions.csv")) == 3
    assert list(read("orphans.csv")["txn_ref"]) == ["P9"]
    assert list(read("exception_queue.csv")["record_ref"]) == ["ORD2024-0000003", "P9", "ORD2024-0000002"]
    assert read("funnel.csv")["decided"].sum() == 3
    assert "decision" in read("triage_queue.csv").columns
    manifest = json.loads(bundle["manifest.json"])
    assert manifest["metrics"]["orphans"] == {"ORPHAN_PAYMENT": 1, "ORPHAN_SHIPMENT": 0}
