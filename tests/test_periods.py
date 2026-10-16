"""Periods with a cut-off (ADR 0002): money not yet due is OPEN, and open items carry into the next run."""

import io
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from recon.acknowledgement import acknowledge_warnings
from recon.config import Channel, FeedSpec, Period, Policy, RunConfig
from recon.evaluation import attach_truth, metrics
from recon.pipeline import inspect, load_config, run

from test_funnel_order import BANK_HEADER
from test_pipeline import acknowledged

ROOT = Path(__file__).parents[1]
OMS_HEADER = "order_id,tracking_no,payment_method,order_amount,ordered_at,order_status,channel\n"
SHIP_HEADER = "tracking_code,client_order_code,carrier,status,cod_collected,ship_fee,cod_fee,remittance_id,picked_at,delivered_at\n"
FEEDS = {"oms": FeedSpec("oms.csv"), "bank": FeedSpec("bank.csv"), "logistics": FeedSpec("ship.csv")}
CHANNELS = {"website": Channel(cod_remittances=True, payout_weekdays=(0, 2, 4), ship_days=7)}
PERIOD = Period(cutoff="2024-01-31", grace_days=3)


def oid(n: int) -> str:
    return f"ORD2024-{n:07d}"


def order(n: int, day: int, method: str = "PREPAID", amount: int = 100) -> str:
    return f"{oid(n)},T{n},{method},{amount},2024-01-{day:02d}T10:00:00,delivered,website\n"


def shipment(n: int, day: int, cod: int = 0, cod_fee: int = 0, remittance: str = "", delivered: bool = True) -> str:
    when = f"2024-01-{day:02d}T08:00:00"
    status = "DELIVERED" if delivered else "SHIPPED"
    return f"T{n},{oid(n)},GHN,{status},{cod},5,{cod_fee},{remittance},{when},{when if delivered else ''}\n"


def transfer(ref: str, n: int, on: str, amount: int = 100) -> str:
    return f"{ref},CK THANH TOAN {oid(n)},{amount},0,{amount},{on}\n"


def write(folder: Path, orders: list[str], ships: list[str], bank: list[str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "oms.csv").write_text(OMS_HEADER + "".join(orders), encoding="utf-8")
    (folder / "ship.csv").write_text(SHIP_HEADER + "".join(ships), encoding="utf-8")
    (folder / "bank.csv").write_text(BANK_HEADER + "".join(bank), encoding="utf-8")


def settle(folder: Path, orders: list[str], ships: list[str], bank: list[str], period: Period | None = PERIOD,
           open_items: str | None = None):
    write(folder, orders, ships, bank)
    config = RunConfig(feeds=FEEDS, policy=Policy(mdr_bps=0, cod_fee_bps=100), channels=CHANNELS, period=period,
                       open_items=open_items)
    outcome = run(folder, acknowledged(folder, config))
    assert not outcome.blocked, [i.message for i in outcome.open_issues]
    return outcome


def unit(outcome, n: int) -> pd.Series:
    return outcome.results.set_index("order_id").loc[oid(n)]


def test_money_not_yet_due_at_the_cut_off_is_open_and_overdue_money_is_missing(tmp_path):
    outcome = settle(tmp_path, [order(1, day=30), order(2, day=10)], [shipment(1, 30), shipment(2, 11)], [])

    recent, old = unit(outcome, 1), unit(outcome, 2)
    assert (recent["decision"], recent["reason_codes"], recent["due_date"]) == ("OPEN", "NOT_YET_DUE", "2024-02-02")
    assert recent["explanation"].startswith("Its money is not due until 2024-02-02 (cut-off 2024-01-31): an open item")
    assert (old["decision"], old["reason_codes"]) == ("EXCEPTION", "MISSING_PAYMENT")
    assert "It was due by 2024-01-13." in old["explanation"]
    assert list(outcome.open_items["order_id"]) == [oid(1)]
    assert outcome.manifest["metrics"]["open_items"]["open"] == 1


def test_a_cod_order_on_a_remittance_after_the_cut_off_is_open_and_so_is_its_batch(tmp_path):
    orders = [order(1, day=29, method="COD"), order(2, day=5, method="COD")]
    ships = [shipment(1, 30, cod=100, cod_fee=1, remittance="GHN-1"), shipment(2, 8, cod=100, cod_fee=1)]
    outcome = settle(tmp_path, orders, ships, [])

    # delivered Tue 30 Jan -> remitted Wed 31 Jan, due by 3 Feb; delivered Mon 8 Jan -> due by Wed 10 + 3 days
    assert (unit(outcome, 1)["decision"], unit(outcome, 1)["due_date"]) == ("OPEN", "2024-02-03")
    assert (unit(outcome, 2)["decision"], unit(outcome, 2)["reason_codes"]) == ("EXCEPTION", "MISSING_PAYMENT")
    remittance = outcome.settlement_batches.set_index("payout_id").loc["GHN-1"]
    assert (remittance["decision"], remittance["reason_codes"]) == ("OPEN", "NOT_YET_DUE")


def test_a_shipment_not_yet_due_is_open_and_an_overdue_one_is_missing(tmp_path):
    orders = [order(1, day=29), order(2, day=5)]
    outcome = settle(tmp_path, orders, [], [transfer("P1", 1, "2024-01-29"), transfer("P2", 2, "2024-01-05")])

    assert (unit(outcome, 1)["decision"], unit(outcome, 1)["waiting_for"]) == ("OPEN", "shipment")
    assert unit(outcome, 1)["explanation"].startswith("Not shipped yet, and not due to ship until 2024-02-08")
    assert (unit(outcome, 2)["decision"], unit(outcome, 2)["reason_codes"]) == ("EXCEPTION", "MISSING_SHIPMENT")


def test_without_a_cut_off_missing_money_is_an_exception_as_before(tmp_path):
    outcome = settle(tmp_path, [order(1, day=30)], [shipment(1, 30)], [], period=None)

    assert (unit(outcome, 1)["decision"], unit(outcome, 1)["reason_codes"]) == ("EXCEPTION", "MISSING_PAYMENT")
    assert outcome.open_items.empty


def test_the_next_run_clears_the_open_items_it_carries_in_and_their_credits_are_not_orphans(tmp_path):
    january = [shipment(1, 30), shipment(2, 30)]  # still on February's report: no remittance has settled them
    first = settle(tmp_path / "january", [order(1, day=30), order(2, day=30)], january, [])
    second = tmp_path / "february"
    second.mkdir()
    (second / "open_items.csv").write_text(first.bundle()["open_items.csv"], encoding="utf-8")

    outcome = settle(second, [order(3, day=10)], [*january, shipment(3, 11)],
                     [transfer("P1", 1, "2024-02-01"), transfer("P3", 3, "2024-02-10")],
                     period=replace(PERIOD, cutoff="2024-02-29"), open_items="open_items.csv")

    assert (unit(outcome, 1)["decision"], unit(outcome, 1)["carried_forward"]) == ("AUTO_ACCEPT", True)
    assert (unit(outcome, 2)["decision"], unit(outcome, 2)["reason_codes"]) == ("EXCEPTION", "MISSING_PAYMENT")
    assert outcome.orphans.empty
    assert outcome.manifest["metrics"]["open_items"] == {"open": 0, "carried_in": 2, "cleared": 1, "still_open": 0,
                                                         "queued": 1}
    assert outcome.manifest["input_files"]["open_items"]["path"] == "open_items.csv"


def test_metrics_score_four_decisions():
    results = pd.DataFrame({"order_id": ["A", "B", "C", "D"], "decision": ["AUTO_ACCEPT", "OPEN", "REVIEW", "OPEN"],
                            "reason_codes": ["THREE_WAY_TIE_OUT", "NOT_YET_DUE", "LINK_CANDIDATES", "NOT_YET_DUE"]})
    truth = pd.DataFrame({"order_id": ["A", "B", "C", "D"], "scenario": "x", "sub_kind": "x",
                          "expected_outcome": ["PASS_1", "OPEN", "REVIEW", "EXCEPTION"],
                          "expected_exception": ["", "NOT_YET_DUE", "LINK_CANDIDATES", "MISSING_PAYMENT"]})

    scored = metrics(attach_truth(results, truth))

    assert scored["accuracy"] == 0.75
    assert scored["decisions"] == {"AUTO_ACCEPT": {"AUTO_ACCEPT": 1}, "REVIEW": {"REVIEW": 1}, "EXCEPTION": {"OPEN": 1},
                                   "OPEN": {"OPEN": 1}}


def test_a_period_and_payment_terms_survive_the_config_round_trip():
    config = RunConfig(feeds=FEEDS, channels=CHANNELS, period=PERIOD, open_items="open_items.csv")

    assert RunConfig.from_dict(config.to_dict()) == config
    with pytest.raises(ValueError, match="not an ISO date"):
        Period(cutoff="31/01/2024")
    with pytest.raises(ValueError, match="payout_weekdays"):
        Channel(payout_weekdays=(7,))


def test_the_chained_set_carries_period_one_open_items_into_period_two():
    runs = []
    for period in ("period_1", "period_2"):
        folder = ROOT / "test" / "vn" / "d_two_periods" / period
        config = load_config(folder / "config.json")
        runs.append(run(folder, acknowledge_warnings(config, inspect(folder, config), "test")))
    first, second = runs
    carried = pd.read_csv(io.StringIO(first.bundle()["open_items.csv"]))["order_id"]

    assert second.manifest["metrics"]["open_items"]["carried_in"] == len(carried) > 0
    assert set(second.results.loc[second.results["carried_forward"], "order_id"]) == set(carried)
    assert first.manifest["metrics"]["accuracy"] == second.manifest["metrics"]["accuracy"] == 1.0
    orphan_orders = set(second.orphans.get("client_order_code", pd.Series(dtype=str)).dropna())
    assert not orphan_orders & set(carried)
