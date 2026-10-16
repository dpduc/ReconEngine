"""Marketplace payouts without a settlement report: REVIEW with candidates, a bounded search and a channel tie-out."""

import json
import time
from pathlib import Path

import pandas as pd
import pytest

from recon.acknowledgement import acknowledge_warnings
from recon.channeldata import labels_for
from recon.config import MARKETPLACE, Channel, FeedSpec, Policy, RunConfig
from recon.funnel.payout_search import MAX_POOL, find_sets
from recon.pipeline import inspect, run

from test_funnel_order import BANK_HEADER, SHIP_HEADER
from test_pipeline import acknowledged

ROOT = Path(__file__).parents[1]
OMS_HEADER = "order_id,tracking_no,payment_method,order_amount,item_total,ordered_at,order_status,channel\n"
PATTERN = r"SHOPEE THANH TOAN DOANH THU (?P<payout>\d+)"
CHANNELS = {"shopee": Channel(chain=MARKETPLACE, payout_pattern=PATTERN, fee_bps=1_000, settlement_days=30),
            "website": Channel()}


def order(n: int, item: int, channel: str = "shopee") -> str:
    return f"ORD-{n},SPX{n},PREPAID,{item},{item},2024-01-0{n}T10:00:00,delivered,{channel}\n"


def payout(amount: int, ref: str = "B1", payout_id: str = "2024011001") -> str:
    return f"{ref},SHOPEE THANH TOAN DOANH THU {payout_id},{amount},0,{amount},2024-01-10\n"


ORDERS = [order(1, 100), order(2, 200), order(3, 300)]  # nets 90, 180 and 270 at 10%


def settle(folder: Path, orders: list[str], bank: list[str]):
    (folder / "oms.csv").write_text(OMS_HEADER + "".join(orders), encoding="utf-8")
    (folder / "bank.csv").write_text(BANK_HEADER + "".join(bank), encoding="utf-8")
    (folder / "ship.csv").write_text(SHIP_HEADER, encoding="utf-8")
    config = RunConfig(feeds={"oms": FeedSpec("oms.csv"), "bank": FeedSpec("bank.csv"), "logistics": FeedSpec("ship.csv")},
                       policy=Policy(mdr_bps=0, cod_fee_bps=0), channels=CHANNELS)
    outcome = run(folder, acknowledged(folder, config))
    assert not outcome.blocked, [i.message for i in outcome.open_issues]
    return outcome


def test_a_payout_without_a_report_is_reviewed_with_the_order_sets_that_add_up_to_it(tmp_path):
    outcome = settle(tmp_path, ORDERS, [payout(450)])  # only orders 2 + 3 net 450

    batch = outcome.settlement_batches.set_index("txn_ref").loc["B1"]
    assert (batch["decision"], batch["reason_codes"], batch["candidate_sets"]) == ("REVIEW", "SETTLEMENT_REPORT_MISSING",
                                                                                 "ORD-3+ORD-2")
    assert batch["candidate_orders"] == "ORD-1|ORD-2|ORD-3"
    units = outcome.results.set_index("order_id")
    assert set(units["decision"]) == {"REVIEW"} and set(units["reason_codes"]) == {"SETTLEMENT_REPORT_MISSING"}
    assert units.loc["ORD-2", "payout_candidates"] == "B1:0.5000"
    assert "No settlement report for shopee" in units.loc["ORD-1", "explanation"]
    assert outcome.orphans.empty


def test_a_payout_credit_is_never_linked_to_another_order_by_amount(tmp_path):
    outcome = settle(tmp_path, [*ORDERS, order(4, 450, channel="website")], [payout(450)])

    website = outcome.results.set_index("order_id").loc["ORD-4"]
    assert pd.isna(website["txn_ref"]) and website["reason_codes"] == "MISSING_PAYMENT"


def test_the_channel_ties_out_its_payout_credits_against_the_expected_net_of_its_orders(tmp_path):
    outcome = settle(tmp_path, ORDERS, [payout(450), payout(80, ref="B2", payout_id="2024011002")])

    assert outcome.manifest["metrics"]["channel_tie_outs"]["shopee"] == {
        "payout_credits": 2, "credited": 530.0, "orders_in_review": 3, "expected_net": 540.0, "difference": -10.0,
        "orders_not_covered": 0, "expected_net_not_covered": 0.0}


def test_the_search_is_bounded_and_says_why_it_stopped():
    assert find_sets([(f"O{i}", 1.0) for i in range(MAX_POOL + 1)], 3, 0)[1].startswith(f"{MAX_POOL + 1} candidate")
    assert find_sets([(f"O{i}", 1.0) for i in range(6)], 2, 0)[1].startswith("more than 3 order sets")
    started = time.perf_counter()
    sets, reason = find_sets([(f"O{i}", 2.0) for i in range(MAX_POOL)], 25, 0)  # odd target, even nets: no set
    assert (sets, reason.startswith("search stopped at its bound")) == ([], True)
    assert time.perf_counter() - started < 3


def test_a_marketplace_channel_names_a_report_or_a_payout_pattern():
    with pytest.raises(ValueError, match="names its settlement report"):
        Channel(chain=MARKETPLACE)
    with pytest.raises(ValueError, match="only a marketplace channel"):
        Channel(payout_pattern=PATTERN)
    config = RunConfig(feeds={"oms": FeedSpec("o.csv"), "bank": FeedSpec("b.csv"), "logistics": FeedSpec("l.csv")},
                       channels=CHANNELS)
    assert RunConfig.from_dict(config.to_dict()) == config


def test_the_marketplace_set_without_its_reports_reviews_every_payout_and_ranks_the_true_one():
    folder = ROOT / "test" / "vn" / "b_marketplace_heavy"
    data = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    for name, label in (("shopee", "SHOPEE"), ("tiktok", "TIKTOK SHOP")):
        data["feeds"].pop(name)
        data["mapping"].pop(name)
        data["channels"][name].pop("settlement")
        data["channels"][name]["payout_pattern"] = rf"{label} THANH TOAN DOANH THU (?P<payout>\d+)"
    config = RunConfig.from_dict(data)
    outcome = run(folder, acknowledge_warnings(config, inspect(folder, config), "test"), labels=labels_for(folder))
    market = outcome.results[outcome.results["channel"].isin(["shopee", "tiktok"])]
    links = pd.read_csv(labels_for(folder) / "links.csv", dtype=str, keep_default_na=False)
    payout_of = links[links["link_kind"].eq("payout_batch")].set_index("order_id")["record_key"]

    assert not market["decision"].eq("AUTO_ACCEPT").any()
    payouts = outcome.settlement_batches[outcome.settlement_batches["channel"].isin(["shopee", "tiktok"])]
    assert len(payouts) == 19 and set(payouts["reason_codes"]) == {"SETTLEMENT_REPORT_MISSING"}
    assert outcome.orphans["reason_codes"].eq("ORPHAN_PAYMENT").sum() <= 3  # the non-sales credits only
    listed = market[market["payout_candidates"].notna()]
    true = listed["order_id"].map(payout_of)
    refs = listed["payout_candidates"].str.split("|").map(lambda items: [i.rsplit(":", 1)[0] for i in items])
    assert all(ref in candidates for ref, candidates in zip(true, refs) if isinstance(ref, str))
    top = sum(candidates[0] == ref for ref, candidates in zip(true, refs) if isinstance(ref, str)) / true.notna().sum()
    assert top > 0.35  # 0.40 at the time of writing: the payout nearest the expected release is right this often
