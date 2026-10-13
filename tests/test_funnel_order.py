"""Funnel order through the public seam: eligibility before evidence, then rule checks in order."""

from pathlib import Path

from recon.config import FeedSpec, Policy, RunConfig
from recon.pipeline import run

OMS_HEADER = "order_id,tracking_no,payment_method,order_amount,ordered_at,order_status\n"
BANK_HEADER = "txn_ref,description,credit_amount,fee_amount,net_amount,value_date\n"
SHIP_HEADER = "tracking_code,client_order_code,cod_collected,ship_fee,cod_fee,picked_at,delivered_at\n"


def order(n: int, amount: str = "100.00", status: str = "delivered", method: str = "PREPAID") -> str:
    return f"ORD2024-{n:07d},T{n},{method},{amount},2024-01-01T10:00:00,{status}\n"


def payment(n: int, credit: str = "100.00", fee: str = "0") -> str:
    return f"P{n},PAYMENT ORD2024-{n:07d},{credit},{fee},{credit},2024-01-02\n"


def shipment(n: int, cod: str = "0", cod_fee: str = "0",
             picked: str = "2024-01-02T08:00:00", delivered: str = "2024-01-03T08:00:00") -> str:
    return f"T{n},ORD2024-{n:07d},{cod},5.0,{cod_fee},{picked},{delivered}\n"


def run_feeds(folder: Path, oms: list[str], bank: list[str], ship: list[str], **policy):
    (folder / "oms.csv").write_text(OMS_HEADER + "".join(oms), encoding="utf-8")
    (folder / "bank.csv").write_text(BANK_HEADER + "".join(bank), encoding="utf-8")
    (folder / "ship.csv").write_text(SHIP_HEADER + "".join(ship), encoding="utf-8")
    config = RunConfig(
        feeds={"oms": FeedSpec("oms.csv"), "bank": FeedSpec("bank.csv"), "logistics": FeedSpec("ship.csv")},
        policy=Policy(**{"mdr_bps": 0, "cod_fee_bps": 0, **policy}),
    )
    outcome = run(folder, config)
    assert not outcome.blocked, [i.message for i in outcome.open_issues]
    return outcome


def by_order(outcome) -> dict[int, tuple[str, str, str]]:
    """order number -> (decision, reason_codes, deciding_step)."""
    return {
        int(row.order_id[-7:]): (row.decision, row.reason_codes, row.deciding_step)
        for row in outcome.results.itertuples()
    }


def test_missing_amount_is_decided_before_status_and_payment(tmp_path):
    outcome = run_feeds(tmp_path, oms=[order(1, amount="", status="canceled")], bank=[], ship=[])
    decision, reasons, step = by_order(outcome)[1]
    assert reasons == "MISSING_SOURCE_EVIDENCE"
    assert decision == "EXCEPTION"
    assert step == "missing_source_evidence"


FULL = ["payment", "shipment"]


def test_canceled_order_without_shipment_needs_a_status_policy(tmp_path):
    outcome = run_feeds(tmp_path, oms=[order(1, status="canceled")], bank=[payment(1)], ship=[])
    assert by_order(outcome)[1] == ("EXCEPTION", "ORDER_STATUS_REQUIRES_POLICY", "status_policy")


def test_unknown_status_defaults_to_pending_policy(tmp_path):
    outcome = run_feeds(tmp_path, oms=[order(1, status="lost_in_space"), order(2, status="")],
                        bank=[payment(1), payment(2)], ship=[shipment(1), shipment(2)])
    assert by_order(outcome)[1][1] == "ORDER_STATUS_REQUIRES_POLICY"
    assert by_order(outcome)[2][1] == "ORDER_STATUS_REQUIRES_POLICY"


def test_status_policy_decides_which_evidence_is_required(tmp_path):
    oms, bank = [order(1, status="canceled")], [payment(1)]
    payment_only = run_feeds(tmp_path, oms, bank, [],
                             status_policy={"delivered": FULL, "shipped": FULL, "canceled": ["payment"]})
    assert by_order(payment_only)[1][0] == "AUTO_ACCEPT"

    full = run_feeds(tmp_path, oms, bank, [], status_policy={"canceled": FULL})
    assert by_order(full)[1][:2] == ("EXCEPTION", "MISSING_SHIPMENT")


LATE_PICKUP = {"picked": "2024-01-04T08:00:00", "delivered": "2024-01-03T08:00:00"}


def test_each_rule_check_decides_its_units_in_order(tmp_path):
    # Each unit also fails every *later* check, so only the documented order explains its code.
    outcome = run_feeds(
        tmp_path,
        oms=[order(n) for n in (1, 2, 3, 4, 6, 7)] + [order(5, method="COD")],
        bank=[payment(2, credit="105.00"), payment(3), payment(4, fee="3"), payment(5, credit="93.00"),
              payment(6), payment(7)],
        ship=[shipment(4, **LATE_PICKUP), shipment(5, cod="100", cod_fee="2", **LATE_PICKUP),
              shipment(6, **LATE_PICKUP), shipment(7)],
    )
    assert by_order(outcome) == {
        1: ("EXCEPTION", "MISSING_PAYMENT", "missing_payment"),
        2: ("EXCEPTION", "AMOUNT_MISMATCH", "amount_mismatch"),
        3: ("EXCEPTION", "MISSING_SHIPMENT", "missing_shipment"),
        4: ("EXCEPTION", "FEE_MISMATCH", "fee_mismatch"),
        5: ("EXCEPTION", "COD_FEE_MISMATCH", "cod_fee_mismatch"),
        6: ("EXCEPTION", "TIMING_MISMATCH", "timing_mismatch"),
        7: ("AUTO_ACCEPT", "THREE_WAY_TIE_OUT", "decision"),
    }
    exceptions = outcome.results[outcome.results["decision"] == "EXCEPTION"].set_index("reason_codes")
    assert exceptions.loc["MISSING_PAYMENT", "priority"] > exceptions.loc["TIMING_MISMATCH", "priority"]


def test_one_real_or_five_percent_amount_error_is_caught_by_default(tmp_path):
    outcome = run_feeds(tmp_path, oms=[order(1, amount="20.00"), order(2, amount="20.00")],
                        bank=[payment(1, credit="21.00"), payment(2, credit="20.01")],
                        ship=[shipment(1), shipment(2)])
    assert by_order(outcome)[1][1] == "AMOUNT_MISMATCH"
    assert by_order(outcome)[2][0] == "AUTO_ACCEPT"  # within the absolute 0.01 tolerance


def test_funnel_counts_follow_step_order_and_add_up(tmp_path):
    outcome = run_feeds(
        tmp_path,
        oms=[order(1, amount=""), order(2, status="canceled"), order(3), order(4)],
        bank=[payment(2), payment(4)],
        ship=[shipment(4)],
    )
    funnel = outcome.funnel.set_index("step")
    assert list(funnel.index) == [
        "missing_source_evidence", "status_policy", "link_exact", "link_constrained", "link_candidates",
        "missing_payment", "amount_mismatch", "missing_shipment", "fee_mismatch", "cod_fee_mismatch",
        "timing_mismatch", "decision",
    ]
    assert funnel["decided"].to_dict() == {
        "missing_source_evidence": 1, "status_policy": 1, "link_exact": 0, "link_constrained": 0,
        "link_candidates": 0, "missing_payment": 1,
        "amount_mismatch": 0, "missing_shipment": 0, "fee_mismatch": 0, "cod_fee_mismatch": 0,
        "timing_mismatch": 0, "decision": 1,
    }
    assert (funnel["units_in"] == funnel["decided"] + funnel["passed_on"]).all()
    assert (funnel["passed_on"].iloc[:-1].to_numpy() == funnel["units_in"].iloc[1:].to_numpy()).all()
    assert funnel["decided"].sum() == len(outcome.results) == 4
