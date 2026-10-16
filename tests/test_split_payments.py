"""Split payments: several credits for one order are linked as a group and compared as a total."""

from pathlib import Path

import pandas as pd

from recon.acknowledgement import acknowledge_warnings
from recon.config import FeedSpec, Policy, RunConfig
from recon.evaluation import link_metrics
from recon.pipeline import inspect, load_config, run

from test_funnel_order import BANK_HEADER, SHIP_HEADER, shipment
from test_pipeline import acknowledged

ROOT = Path(__file__).parents[1]
OMS_HEADER = "order_id,tracking_no,payment_method,order_amount,ordered_at,order_status,customer_name\n"
FEEDS = {"oms": FeedSpec("oms.csv"), "bank": FeedSpec("bank.csv"), "logistics": FeedSpec("ship.csv")}
POLICY = Policy(mdr_bps=0, cod_fee_bps=0, payer_pattern=r"^(?P<payer>[A-Z ]+?) chuyen tien")


def oid(n: int) -> str:
    return f"ORD2024-{n:07d}"


def order(n: int, amount: int = 100, name: str = "An Nguyen") -> str:
    return f"{oid(n)},T{n},PREPAID,{amount},2024-01-01T10:00:00,delivered,{name}\n"


def credit(ref: str, amount: int, note: str = "", day: int = 1, payer: str = "AN NGUYEN") -> str:
    return f"{ref},{payer} chuyen tien {note}".rstrip() + f",{amount},0,{amount},2024-01-{day:02d}\n"


def settle(folder: Path, orders: list[str], bank: list[str]):
    (folder / "oms.csv").write_text(OMS_HEADER + "".join(orders), encoding="utf-8")
    (folder / "bank.csv").write_text(BANK_HEADER + "".join(bank), encoding="utf-8")
    (folder / "ship.csv").write_text(SHIP_HEADER + "".join(shipment(int(o[8:15])) for o in orders), encoding="utf-8")
    outcome = run(folder, acknowledged(folder, RunConfig(feeds=FEEDS, policy=POLICY)))
    assert not outcome.blocked, [i.message for i in outcome.open_issues]
    return outcome


def unit(outcome, n: int = 1) -> pd.Series:
    return outcome.results.set_index("order_id").loc[oid(n)]


def test_an_order_paid_in_two_credits_naming_it_ties_out_against_their_sum(tmp_path):
    outcome = settle(tmp_path, [order(1)], [credit("P1", 40, f"coc {oid(1)}"), credit("P2", 60, oid(1), day=2)])

    paid = unit(outcome)
    assert (paid["decision"], paid["reason_codes"], paid["bank_link_pass"]) == ("AUTO_ACCEPT", "THREE_WAY_TIE_OUT",
                                                                             "exact")
    assert (paid["txn_ref"], paid["credit_amount"], paid["bank_split_rows"]) == ("P1|P2", 100, "1|2")
    assert paid["explanation"].startswith("Order, bank credits (P1, P2: paid in 2 parts) and shipment tie out.")
    assert outcome.orphans.empty


def test_a_missing_part_is_an_amount_mismatch_listing_every_credit(tmp_path):
    outcome = settle(tmp_path, [order(1)], [credit("P1", 40, f"coc {oid(1)}"), credit("P2", 30, oid(1), day=2)])

    short = unit(outcome)
    assert (short["decision"], short["reason_codes"]) == ("EXCEPTION", "AMOUNT_MISMATCH")
    assert short["explanation"].startswith("The total of the 2 bank credits (P1, P2) is 30.00 less than expected.")


def test_two_credits_that_each_pay_the_order_are_a_duplicate_payment(tmp_path):
    outcome = settle(tmp_path, [order(1)], [credit("P1", 100, oid(1)), credit("P2", 100, oid(1))])

    paid_twice = unit(outcome)
    assert (paid_twice["decision"], paid_twice["reason_codes"]) == ("EXCEPTION", "DUPLICATE_PAYMENT")
    assert "one of them alone pays the order: a duplicate payment" in paid_twice["explanation"]


def test_a_customers_credits_without_a_reference_link_when_together_they_pay_the_order(tmp_path):
    outcome = settle(tmp_path, [order(1)], [credit("P1", 40, "coc"), credit("P2", 60, "ck mua hang", day=2)])

    paid = unit(outcome)
    assert (paid["decision"], paid["bank_link_pass"], paid["txn_ref"]) == ("AUTO_ACCEPT", "constrained_split", "P1|P2")


def test_a_payer_split_is_not_linked_when_another_open_order_of_the_customer_could_own_its_credits(tmp_path):
    outcome = settle(tmp_path, [order(1), order(2, amount=70)], [credit("P1", 40, "coc"), credit("P2", 60, day=2)])

    assert set(outcome.results["bank_link_pass"].dropna()) == set()


def test_a_payer_split_is_not_linked_when_the_customers_credits_do_not_add_up(tmp_path):
    outcome = settle(tmp_path, [order(1)], [credit("P1", 40, "coc"), credit("P2", 60, day=2), credit("P3", 25, day=2)])

    assert pd.isna(unit(outcome)["bank_link_pass"])


def test_link_metrics_score_each_credit_of_a_split(tmp_path):
    outcome = settle(tmp_path, [order(1)], [credit("P1", 40, "coc"), credit("P2", 60, day=2)])
    truth = pd.DataFrame({"feed": ["bank", "bank"], "record_key": ["P1", "P2"], "order_id": [oid(1)] * 2,
                          "link_kind": ["split", "split"]})

    links = link_metrics(outcome.results, truth)

    assert links["per_link_kind"]["bank:split"] == {"true_links": 2, "found": 2, "recall": 1.0}
    assert links["per_pass"]["bank:constrained_split"] == {"links": 2, "correct": 2, "precision": 1.0}


def test_the_social_set_auto_accepts_every_split_payment():
    folder = ROOT / "test" / "vn" / "c_social_heavy"
    config = load_config(folder / "config.json")
    outcome = run(folder, acknowledge_warnings(config, inspect(folder, config), "test"))
    codes = outcome.results.groupby("scenario")["reason_codes"].agg(set).to_dict()

    assert codes["SPLIT_PAYMENT"] == {"THREE_WAY_TIE_OUT"}
    assert codes["DUPLICATE_PAYMENT"] == {"DUPLICATE_PAYMENT"}
    assert outcome.manifest["metrics"]["links"]["per_link_kind"]["bank:split"]["recall"] == 1.0
