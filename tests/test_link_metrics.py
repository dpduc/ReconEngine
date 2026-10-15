"""Link-level metrics and amount-weighted reason-code metrics, scored against supplied truth."""

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from recon.evaluation import link_metrics, per_reason_code, read_link_truth
from recon.olistdata import generate_from_olist
from recon.pipeline import load_config, run

from test_linking import OMS_TWO, SHIP_TWO, write_sources
from test_pipeline import acknowledged

ROOT = Path(__file__).parents[1]
LINKS_HEADER = "feed,record_key,order_id,link_kind,note\n"


def _run_with_links(folder: Path, config, links: str):
    (folder / "links.csv").write_text(LINKS_HEADER + links, encoding="utf-8")
    outcome = run(folder, acknowledged(folder, replace(config, link_truth="links.csv")))
    assert not outcome.blocked, outcome.open_issues
    return outcome


def test_a_wrong_constrained_link_is_a_link_false_positive(tmp_path):
    # P2 matches order 2 on amount and date, but it is a refund from a supplier, not this order's payment.
    config = write_sources(tmp_path, oms=OMS_TWO, ship=SHIP_TWO,
                           bank=("P1,PAYMENT ORD2024-0000001,100.00,0,100.00,2024-01-02\n"
                                 "P2,TRANSFER RECEIVED,42.50,0,42.50,2024-01-13\n"))

    outcome = _run_with_links(tmp_path, config, (
        "bank,P1,ORD2024-0000001,order_ref,\n"
        "bank,P2,,none,NOT_A_SALE\n"
        "logistics,T1,ORD2024-0000001,order_ref,\n"
        "logistics,T2,ORD2024-0000002,order_ref,\n"))

    links = outcome.manifest["metrics"]["links"]
    assert outcome.results.set_index("order_id").loc["ORD2024-0000002", "bank_link_pass"] == "constrained"
    assert links["per_pass"]["bank:constrained"] == {"links": 1, "correct": 0, "precision": 0.0}
    assert links["per_pass"]["bank:exact"] == {"links": 1, "correct": 1, "precision": 1.0}
    assert links["per_feed"]["bank"]["precision"] == 0.5 and links["per_feed"]["bank"]["recall"] == 1.0
    assert links["overall"] == {"links": 4, "correct": 3, "precision": 0.75, "true_links": 3, "found": 3,
                                "recall": 1.0}


def test_recall_is_per_true_link_kind_and_unlinkable_truth_is_not_scored(tmp_path):
    config = write_sources(tmp_path, oms=OMS_TWO, ship=SHIP_TWO,
                           bank="P1,PAYMENT ORD2024-0000001,100.00,0,100.00,2024-01-02\n")

    links = _run_with_links(tmp_path, config, (
        "bank,P1,ORD2024-0000001,order_ref,\n"
        "bank,PAYOUT-9,ORD2024-0000002,payout_batch,\n"  # paid in a batch the engine cannot see yet
        "shopee,SHP-2,ORD2024-0000002,order_no,\n"  # a feed the run does not read
        "bank,P0,ORD2023-0009999,order_ref,PRIOR_PERIOD\n"  # an order outside this run
        "logistics,T1,ORD2024-0000001,order_ref,\n"
        "logistics,T2,ORD2024-0000002,order_ref,\n")).manifest["metrics"]["links"]

    assert links["per_link_kind"]["bank:order_ref"] == {"true_links": 1, "found": 1, "recall": 1.0}
    assert links["per_link_kind"]["bank:payout_batch"] == {"true_links": 1, "found": 0, "recall": 0.0}
    assert links["not_scored"] == {"feed_not_linked": 1, "order_not_in_run": 1}


def test_review_candidates_are_scored_separately_from_links():
    results = pd.DataFrame({"order_id": ["O1", "O2"], "txn_ref": [None, None], "bank_link_pass": [None, None],
                            "tracking_code": [None, None], "logistics_link_pass": [None, None],
                            "link_candidates": ["B2:0.6000|B1:0.4000", "B3:0.6000"]})
    truth = pd.DataFrame({"feed": ["bank", "bank"], "record_key": ["B1", "B9"], "order_id": ["O1", "O2"]})

    links = link_metrics(results, truth)

    assert links["candidates"] == {"units": 2, "best_correct": 0.0, "any_correct": 0.5}
    assert links["overall"]["links"] == 0 and links["overall"]["recall"] == 0.0


def test_link_truth_without_its_key_columns_is_rejected():
    with pytest.raises(ValueError, match="record_key"):
        read_link_truth(pd.DataFrame({"feed": ["bank"], "order_id": ["O1"]}))


def test_reason_codes_are_also_weighted_by_money():
    results = pd.DataFrame({
        "order_amount": [1000.0, 10.0, 500.0, 40.0],
        "reason_codes": ["AMOUNT_MISMATCH", "AMOUNT_MISMATCH", "THREE_WAY_TIE_OUT", "AMOUNT_MISMATCH"],
        "truth_exception": ["AMOUNT_MISMATCH", "", "AMOUNT_MISMATCH", "AMOUNT_MISMATCH"],
        "truth_outcome": ["EXCEPTION", "PASS_1", "EXCEPTION", "EXCEPTION"],
    })

    code = per_reason_code(results)["AMOUNT_MISMATCH"]

    assert (code["precision"], code["recall"]) == (2 / 3, 2 / 3)
    assert code["amount_caught"] == 1040.0 and code["amount_missed"] == 500.0
    assert code["amount_precision"] == pytest.approx(1040 / 1050)
    assert code["amount_recall"] == pytest.approx(1040 / 1540)


def test_the_olist_benchmark_links_every_order_in_pass_one(tmp_path):
    generate_from_olist(ROOT / "brazil ecom", tmp_path, rows=300, seed=7)

    links = run(tmp_path, load_config(ROOT / "configs" / "olist_benchmark.json")).manifest["metrics"]["links"]

    assert links["overall"]["precision"] == 1.0 and links["overall"]["recall"] == 1.0
    assert set(links["per_pass"]) == {"bank:exact", "logistics:exact"}
