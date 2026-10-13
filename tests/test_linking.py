"""Linking passes through the public seam: exact reference, constrained match, candidates."""

from pathlib import Path

from recon.config import FeedSpec, Policy, RunConfig
from recon.pipeline import run

OLIST_ID = "e481f51cbdc54678b7cc49136f2d6af7"

OMS_HEADER = "order_id,tracking_no,payment_method,order_amount,ordered_at\n"
BANK_HEADER = "txn_ref,description,credit_amount,fee_amount,net_amount,value_date\n"
SHIP_HEADER = "tracking_code,client_order_code,cod_collected,ship_fee,cod_fee\n"
POLICY = Policy(mdr_bps=0, cod_fee_bps=0, amount_tolerance=0.01)


def write_sources(folder: Path, oms: str, bank: str, ship: str = "", policy: Policy = POLICY,
                  oms_header: str = OMS_HEADER, bank_header: str = BANK_HEADER) -> RunConfig:
    (folder / "oms.csv").write_text(oms_header + oms, encoding="utf-8")
    (folder / "bank.csv").write_text(bank_header + bank, encoding="utf-8")
    (folder / "ship.csv").write_text(SHIP_HEADER + ship, encoding="utf-8")
    return RunConfig(
        feeds={"oms": FeedSpec("oms.csv"), "bank": FeedSpec("bank.csv"), "logistics": FeedSpec("ship.csv")},
        policy=policy,
    )


def results_by_order(folder: Path, config: RunConfig):
    outcome = run(folder, config)
    assert not outcome.blocked, outcome.open_issues
    return outcome.results.set_index("order_id")


def test_bank_without_reference_column_links_through_description(tmp_path):
    config = write_sources(
        tmp_path,
        oms=f"{OLIST_ID},T1,PREPAID,100.00,2024-01-01T10:00:00\n",
        bank=f"P1,PIX RECEBIDO REF {OLIST_ID} LOJA,100.00,0,100.00,2024-01-02\n",
        ship=f"T1,{OLIST_ID},0,5.0,0\n",
    )
    results = results_by_order(tmp_path, config)
    assert results.loc[OLIST_ID, "bank_link_pass"] == "exact"
    assert results.loc[OLIST_ID, "decision"] == "AUTO_ACCEPT"


def test_structured_bank_reference_is_preferred_over_description(tmp_path):
    config = write_sources(
        tmp_path,
        oms="ORD2024-0000001,T1,PREPAID,100.00,2024-01-01T10:00:00\n",
        bank="P1,CARD SETTLEMENT BATCH 77,100.00,0,100.00,2024-01-02,ORD2024-0000001\n",
        ship="T1,ORD2024-0000001,0,5.0,0\n",
        bank_header=BANK_HEADER.strip() + ",order_ref\n",
    )
    results = results_by_order(tmp_path, config)
    assert results.loc["ORD2024-0000001", "bank_link_pass"] == "exact"
    assert results.loc["ORD2024-0000001", "decision"] == "AUTO_ACCEPT"


# Order 1 links by reference; order 2's bank line carries no reference at all.
OMS_TWO = (
    "ORD2024-0000001,T1,PREPAID,100.00,2024-01-01T10:00:00\n"
    "ORD2024-0000002,T2,PREPAID,42.50,2024-01-10T09:00:00\n"
)
SHIP_TWO = "T1,ORD2024-0000001,0,5.0,0\nT2,ORD2024-0000002,0,5.0,0\n"


def test_unique_currency_amount_date_candidate_is_linked_by_pass_2(tmp_path):
    config = write_sources(
        tmp_path,
        oms=OMS_TWO,
        bank=("P1,PAYMENT ORD2024-0000001,100.00,0,100.00,2024-01-02\n"
              "P2,TRANSFER RECEIVED,42.50,0,42.50,2024-01-13\n"),
        ship=SHIP_TWO,
    )
    results = results_by_order(tmp_path, config)
    assert results.loc["ORD2024-0000001", "bank_link_pass"] == "exact"
    assert results.loc["ORD2024-0000002", "bank_link_pass"] == "constrained"
    assert results.loc["ORD2024-0000002", "txn_ref"] == "P2"
    assert "MISSING_PAYMENT" not in results.loc["ORD2024-0000002", "reason_codes"]
    assert len(results.loc["ORD2024-0000002", "input_row_hashes"].split("|")) == 3


def test_record_outside_date_window_is_not_linked_by_pass_2(tmp_path):
    late_bank = ("P1,PAYMENT ORD2024-0000001,100.00,0,100.00,2024-01-02\n"
                 "P2,TRANSFER RECEIVED,42.50,0,42.50,2024-01-15\n")  # 5 days after the order
    config = write_sources(tmp_path, oms=OMS_TWO, bank=late_bank, ship=SHIP_TWO)
    results = results_by_order(tmp_path, config)
    assert results.loc["ORD2024-0000002", "bank_link_pass"] != "constrained"
    assert results.loc["ORD2024-0000002", "decision"] != "AUTO_ACCEPT"

    wider = write_sources(tmp_path, oms=OMS_TWO, bank=late_bank, ship=SHIP_TWO,
                          policy=Policy(mdr_bps=0, cod_fee_bps=0, amount_tolerance=0.01, link_date_window_days=5))
    assert results_by_order(tmp_path, wider).loc["ORD2024-0000002", "bank_link_pass"] == "constrained"


def test_currency_must_agree_for_pass_2(tmp_path):
    # Each feed is single-currency (mixed currency within a feed is a blocking issue);
    # the mismatch is across feeds.
    config = write_sources(
        tmp_path,
        oms=("ORD2024-0000001,T1,PREPAID,100.00,2024-01-01T10:00:00,BRL\n"
             "ORD2024-0000002,T2,PREPAID,42.50,2024-01-10T09:00:00,BRL\n"),
        bank=("P1,PAYMENT ORD2024-0000001,100.00,0,100.00,2024-01-02,USD\n"
              "P2,TRANSFER RECEIVED,42.50,0,42.50,2024-01-11,USD\n"),
        ship=SHIP_TWO,
        oms_header=OMS_HEADER.strip() + ",currency\n",
        bank_header=BANK_HEADER.strip() + ",currency\n",
    )
    results = results_by_order(tmp_path, config)
    assert results.loc["ORD2024-0000001", "bank_link_pass"] == "exact"
    assert results.loc["ORD2024-0000002", "bank_link_pass"] != "constrained"


def test_two_equally_good_candidates_go_to_review_with_both_listed(tmp_path):
    config = write_sources(
        tmp_path,
        oms=OMS_TWO,
        bank=("P1,PAYMENT ORD2024-0000001,100.00,0,100.00,2024-01-02\n"
              "P2,TRANSFER RECEIVED,42.50,0,42.50,2024-01-11\n"
              "P3,TRANSFER RECEIVED,42.50,0,42.50,2024-01-11\n"),
        ship=SHIP_TWO,
    )
    unit = results_by_order(tmp_path, config).loc["ORD2024-0000002"]
    assert unit["decision"] == "REVIEW"
    assert unit["bank_link_pass"] != "constrained"
    assert unit["txn_ref"] != unit["txn_ref"]  # no bank record attached (NaN)
    candidates = dict(c.split(":") for c in unit["link_candidates"].split("|"))
    assert set(candidates) == {"P2", "P3"}
    assert candidates["P2"] == candidates["P3"]
    assert "LINK_CANDIDATES" in unit["reason_codes"]


def test_bank_record_naming_another_order_is_not_a_pass_2_candidate(tmp_path):
    # Order 1 is canceled (decided before linking); its payment must not be borrowed by order 2.
    config = write_sources(
        tmp_path,
        oms=("ORD2024-0000001,T1,PREPAID,42.50,2024-01-10T08:00:00,canceled\n"
             "ORD2024-0000002,T2,PREPAID,42.50,2024-01-10T09:00:00,delivered\n"),
        bank="P1,PAYMENT ORD2024-0000001,42.50,0,42.50,2024-01-11\n",
        ship=SHIP_TWO,
        oms_header=OMS_HEADER.strip() + ",order_status\n",
    )
    unit = results_by_order(tmp_path, config).loc["ORD2024-0000002"]
    assert unit["bank_link_pass"] != "constrained"
    assert unit["reason_codes"] == "MISSING_PAYMENT"


def test_pass_3_candidates_never_auto_accept(tmp_path):
    # Even a policy that would accept anything leaves candidate-only units in REVIEW.
    lax = Policy(mdr_bps=0, cod_fee_bps=0, amount_tolerance=0.01, accept_threshold=0.0)
    config = write_sources(
        tmp_path,
        oms=OMS_TWO,
        bank=("P1,PAYMENT ORD2024-0000001,100.00,0,100.00,2024-01-02\n"
              "P2,TRANSFER RECEIVED,42.50,0,42.50,2024-01-15\n"),  # one candidate, just outside the window
        ship=SHIP_TWO,
        policy=lax,
    )
    results = results_by_order(tmp_path, config)
    unit = results.loc["ORD2024-0000002"]
    assert unit["decision"] == "REVIEW"
    assert unit["deciding_step"] == "link_candidates"
    assert unit["link_candidates"].startswith("P2:")
    assert results.loc["ORD2024-0000001", "decision"] == "AUTO_ACCEPT"
