"""Marketplace payouts: order -> settlement line -> payout credit, with a line check and a batch tie-out."""

import io
from pathlib import Path

import pandas as pd
import pytest

from recon.acknowledgement import acknowledge_warnings
from recon.channeldata import labels_for
from recon.config import MARKETPLACE, Channel, FeedSpec, Policy, RunConfig
from recon.evaluation import link_metrics
from recon.pipeline import inspect, load_config, run

from test_funnel_order import BANK_HEADER, SHIP_HEADER
from test_pipeline import acknowledged

ROOT = Path(__file__).parents[1]
OMS_HEADER = "order_id,tracking_no,payment_method,order_amount,item_total,ordered_at,order_status,channel,channel_order_no\n"
REPORT_HEADER = "order_no,payout_id,gross,fee_commission,refund,net\n"
SHOPEE = {"shopee": Channel(chain=MARKETPLACE, settlement="shopee", fee_bps=1_000), "website": Channel()}


def order(n: int, item: int = 100, shipping: int = 20) -> str:
    return f"ORD-{n},SPX{n},PREPAID,{item + shipping},{item},2024-01-01T10:00:00,delivered,shopee,SHP{n}\n"


def line(n: int, gross: int = 100, fee: int | None = None, refund: int = 0, payout: str = "2024011001") -> str:
    fee = gross // 10 if fee is None else fee
    return f"SHP{n},{payout},{gross},-{fee},{refund},{gross - fee - refund}\n"


def payout(amount: int, payout_id: str = "2024011001", ref: str = "B1") -> str:
    return f"{ref},SHOPEE THANH TOAN DOANH THU {payout_id},{amount},0,{amount},2024-01-10\n"


ORDERS = [order(1), order(2, item=200), order(3, item=300)]
LINES = [line(1), line(2, gross=200), line(3, gross=300)]  # nets 90 + 180 + 270 = 540


def settle(folder: Path, orders: list[str], bank: list[str], lines: list[str], extra_oms: str = ""):
    (folder / "oms.csv").write_text(OMS_HEADER + "".join(orders) + extra_oms, encoding="utf-8")
    (folder / "bank.csv").write_text(BANK_HEADER + "".join(bank), encoding="utf-8")
    (folder / "ship.csv").write_text(SHIP_HEADER, encoding="utf-8")
    (folder / "shopee.csv").write_text(REPORT_HEADER + "".join(lines), encoding="utf-8")
    config = RunConfig(feeds={"oms": FeedSpec("oms.csv"), "bank": FeedSpec("bank.csv"),
                              "logistics": FeedSpec("ship.csv"), "shopee": FeedSpec("shopee.csv", role="settlement")},
                       policy=Policy(mdr_bps=0, cod_fee_bps=0), channels=SHOPEE)
    outcome = run(folder, acknowledged(folder, config))
    assert not outcome.blocked, [i.message for i in outcome.open_issues]
    return outcome


def decisions(outcome) -> dict[str, tuple[str, str]]:
    return {row.order_id: (row.decision, row.reason_codes) for row in outcome.results.itertuples()}


def batch(outcome, payout_id: str = "2024011001") -> pd.Series:
    return outcome.settlement_batches.set_index("payout_id").loc[payout_id]


def test_orders_whose_lines_and_payout_tie_out_are_auto_accepted(tmp_path):
    outcome = settle(tmp_path, ORDERS, [payout(540)], LINES)

    assert decisions(outcome) == {f"ORD-{n}": ("AUTO_ACCEPT", "SETTLEMENT_TIE_OUT") for n in (1, 2, 3)}
    assert set(outcome.results["txn_ref"]) == {"B1"} and set(outcome.results["bank_link_pass"]) == {"payout"}
    assert outcome.orphans.empty  # the payout credit is reported on its batch
    paid = batch(outcome)
    assert (paid["reason_codes"], paid["txn_ref"], paid["lines"], paid["orders"], paid["net"]) == \
        ("BATCH_TIE_OUT", "B1", 3, 3, 540)
    exported = pd.read_csv(io.StringIO(outcome.bundle()["settlement_batches.csv"]), dtype={"payout_id": str})
    assert list(exported["payout_id"]) == ["2024011001"]


def test_a_wrong_fee_is_an_exception_on_the_order_whose_line_carries_it(tmp_path):
    lines = [line(1), line(2, gross=200, fee=30), line(3, gross=300)]  # the payout pays what the report says
    outcome = settle(tmp_path, ORDERS, [payout(530)], lines)

    assert decisions(outcome) == {"ORD-1": ("AUTO_ACCEPT", "SETTLEMENT_TIE_OUT"), "ORD-2": ("EXCEPTION", "FEE_MISMATCH"),
                                  "ORD-3": ("AUTO_ACCEPT", "SETTLEMENT_TIE_OUT")}
    assert (batch(outcome)["reason_codes"], batch(outcome)["exception_orders"]) == ("BATCH_TIE_OUT", "ORD-2")
    unit = outcome.results.set_index("order_id").loc["ORD-2"]
    assert "10.00 more than the channel's terms (10.00% of gross)" in unit["explanation"]


def test_a_payout_credit_that_differs_from_its_lines_net_puts_the_batch_and_its_orders_in_review(tmp_path):
    outcome = settle(tmp_path, ORDERS, [payout(500)], LINES)

    assert (batch(outcome)["reason_codes"], batch(outcome)["tie_out_delta"]) == ("SETTLEMENT_BATCH_MISMATCH", -40)
    assert decisions(outcome) == {f"ORD-{n}": ("REVIEW", "SETTLEMENT_BATCH_MISMATCH") for n in (1, 2, 3)}
    assert "40.00 less than the net of its lines" in outcome.results["explanation"].iloc[0]


def test_a_refunded_line_is_an_amount_mismatch_and_the_batch_still_ties_out(tmp_path):
    lines = [line(1, fee=0, refund=100), line(2, gross=200), line(3, gross=300)]
    outcome = settle(tmp_path, ORDERS, [payout(450)], lines)

    assert decisions(outcome)["ORD-1"] == ("EXCEPTION", "AMOUNT_MISMATCH")
    assert batch(outcome)["reason_codes"] == "BATCH_TIE_OUT"


def test_an_order_without_a_line_or_a_payout_without_a_credit_is_missing_payment(tmp_path):
    outcome = settle(tmp_path, ORDERS + [order(4)], [], LINES)

    assert {d for d in decisions(outcome).values()} == {("EXCEPTION", "MISSING_PAYMENT")}
    explained = outcome.results.set_index("order_id")["explanation"]
    assert explained["ORD-4"].startswith("No line for this order in the shopee settlement report.")
    assert "no bank credit names it" in explained["ORD-1"]
    assert batch(outcome)["reason_codes"] == "MISSING_PAYMENT"


def test_a_payout_credit_is_never_linked_to_another_order_by_amount(tmp_path):
    website = "ORD-9,T9,PREPAID,540,540,2024-01-09T10:00:00,delivered,website,\n"
    outcome = settle(tmp_path, ORDERS, [payout(540)], LINES, extra_oms=website)

    unit = outcome.results.set_index("order_id").loc["ORD-9"]
    assert pd.isna(unit["txn_ref"]) and unit["reason_codes"] == "MISSING_PAYMENT"


def test_link_metrics_score_the_settlement_links(tmp_path):
    outcome = settle(tmp_path, ORDERS, [payout(540)], LINES)
    truth = pd.DataFrame({"feed": ["shopee"] * 3 + ["bank"] * 3, "record_key": ["SHP1", "SHP2", "SHP3", "B1", "B1", "B1"],
                          "order_id": ["ORD-1", "ORD-2", "ORD-3"] * 2,
                          "link_kind": ["order_no"] * 3 + ["payout_batch"] * 3})

    links = link_metrics(outcome.results, truth, settlement_feeds=("shopee",))

    assert links["per_feed"]["shopee"] == {"links": 3, "correct": 3, "precision": 1.0, "true_links": 3, "found": 3,
                                           "recall": 1.0}
    assert links["per_link_kind"]["bank:payout_batch"]["recall"] == 1.0
    assert links["per_pass"]["bank:payout"]["precision"] == 1.0


def test_a_marketplace_channel_names_a_settlement_report():
    feeds = {"oms": FeedSpec("o.csv"), "bank": FeedSpec("b.csv"), "logistics": FeedSpec("l.csv")}
    with pytest.raises(ValueError, match="names its settlement report"):
        Channel(chain=MARKETPLACE)
    with pytest.raises(ValueError, match="is not a settlement report feed"):
        RunConfig(feeds=feeds, channels={"shopee": Channel(chain=MARKETPLACE, settlement="bank")})


def test_the_marketplace_set_auto_accepts_every_correct_payout_and_flags_each_wrong_line():
    folder = ROOT / "test" / "vn" / "b_marketplace_heavy"
    config = load_config(folder / "config.json")
    outcome = run(folder, acknowledge_warnings(config, inspect(folder, config), "test"), labels=labels_for(folder))
    market = outcome.results[outcome.results["channel"].isin(["shopee", "tiktok"])].set_index("scenario")

    assert set(market.loc["MATCHED", "decision"]) == {"AUTO_ACCEPT"}
    assert set(market.loc["FEE_OVERCHARGE", "reason_codes"]) == {"FEE_MISMATCH"}
    assert set(market.loc["RETURN_NOT_IN_OMS", "reason_codes"]) == {"AMOUNT_MISMATCH"}
    assert set(outcome.settlement_batches["reason_codes"]) == {"BATCH_TIE_OUT"}
    links = outcome.manifest["metrics"]["links"]["per_feed"]
    assert links["shopee"]["recall"] == links["tiktok"]["recall"] == 1.0
