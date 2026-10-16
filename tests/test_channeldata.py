"""Multi-channel test datasets: deterministic, every record accounted for, payouts add up."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest

from recon.channeldata import FILES, LABEL_FILES, LABELS, MARKETS, generate, labels_for
from recon.pipeline import inspect, load_config

ROOT = Path(__file__).resolve().parents[1]
COMMITTED = ROOT / "test"  # only the default market's sets are committed
SETS = [(market.code, profile.name) for market in MARKETS.values() for profile in market.profiles]


@pytest.fixture(scope="module")
def sets(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("channel")
    for market in MARKETS:
        generate(ROOT / "brazil ecom", out, market=market, with_baseline=False)
    return out


def _read(folder: Path, name: str) -> pd.DataFrame:
    return pd.read_csv(folder / name, dtype=str, keep_default_na=False)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_generation_is_deterministic_and_matches_the_committed_sets(sets, tmp_path):
    for market in MARKETS:
        generate(ROOT / "brazil ecom", tmp_path, market=market, with_baseline=False)
    for market, profile in SETS:
        files = [Path(market, profile, name) for name in (*FILES, "config.json")]
        files += [Path(market, LABELS, profile, name) for name in LABEL_FILES]
        for path in files:
            fresh = sets / path
            assert _digest(fresh) == _digest(tmp_path / path), path
            committed = COMMITTED / path
            if committed.exists():
                assert _digest(fresh) == _digest(committed), \
                    f"test/{path.as_posix()} is stale: regenerate with python -m recon.channeldata"


def test_labels_are_kept_out_of_the_input_folders(sets):
    for market, profile in SETS:
        folder = sets / market / profile
        assert not any((folder / name).exists() for name in LABEL_FILES)
        assert all((labels_for(folder) / name).is_file() for name in LABEL_FILES)
        config = load_config(folder / "config.json")
        assert config.ground_truth is None and config.link_truth is None


@pytest.mark.parametrize("market,profile", SETS)
def test_every_order_and_record_is_accounted_for(sets, market, profile):
    folder = sets / market / profile
    labels = labels_for(folder)
    oms, truth, links = _read(folder, "oms_orders.csv"), _read(labels, "ground_truth.csv"), _read(labels, "links.csv")
    assert truth["order_id"].is_unique and set(truth["order_id"]) == set(oms["order_id"])
    linked = links.groupby("feed")["record_key"].agg(set)
    assert set(_read(folder, "bank_statement.csv")["txn_ref"]) == linked["bank"]
    assert set(_read(folder, "logistics_report.csv")["tracking_code"]) == linked["logistics"]
    for name, (order_no, _, _) in MARKETS[market].seller.SETTLEMENT_KEYS.items():
        assert set(_read(folder, name)[order_no]) == linked[name.split("_")[0]]
    assert set(links.loc[links["order_id"].eq(""), "note"]) == {"NOT_A_SALE"}  # the rest name their true order
    earlier = links[links["order_id"].ne("") & ~links["order_id"].isin(oms["order_id"])]
    assert set(earlier["note"]) == {"PRIOR_PERIOD"}  # orders outside this OMS export are last month's


@pytest.mark.parametrize("market,profile", SETS)
def test_each_batched_credit_equals_its_parts(sets, market, profile):
    folder = sets / market / profile
    bank = pd.read_csv(folder / "bank_statement.csv", keep_default_na=False).set_index("txn_ref")
    links = _read(labels_for(folder), "links.csv")
    for name, (_, payout, net) in MARKETS[market].seller.SETTLEMENT_KEYS.items():
        rows = pd.read_csv(folder / name, dtype={payout: str})
        for payout_id, total in rows.groupby(payout)[net].sum().items():
            line = bank[bank["description"].str.endswith(payout_id)]
            assert len(line) == 1 and line["credit_amount"].iloc[0] == pytest.approx(total, abs=0.005)
    logistics = pd.read_csv(folder / "logistics_report.csv", keep_default_na=False)
    remitted = (logistics["cod_collected"] - logistics["ship_fee"] - logistics["cod_fee"]).groupby(
        logistics["client_order_code"]).sum()
    for ref, group in links[links["link_kind"].eq("cod_remittance")].groupby("record_key"):
        assert bank.loc[ref, "credit_amount"] == pytest.approx(remitted[group["order_id"]].sum(), abs=0.005)
    orders = pd.read_csv(folder / "oms_orders.csv").set_index("order_id")["order_amount"]
    for order_id, group in links[links["link_kind"].eq("split")].groupby("order_id"):
        assert bank.loc[group["record_key"], "credit_amount"].sum() == pytest.approx(orders[order_id], abs=0.005)


def test_vietnam_amounts_are_whole_dong():
    for profile in MARKETS["vn"].profiles:
        folder = COMMITTED / "vn" / profile.name
        if not folder.exists():
            pytest.skip("run python -m recon.channeldata first")
        oms = pd.read_csv(folder / "oms_orders.csv")
        assert (oms["currency"] == "VND").all() and (oms["order_amount"] % 1_000 == 0).all()
        bank = pd.read_csv(folder / "bank_statement.csv")
        assert pd.api.types.is_integer_dtype(bank["credit_amount"])


@pytest.mark.parametrize("market,profile", SETS)
def test_each_config_loads_and_inspects(sets, market, profile):
    folder = sets / market / profile
    report = inspect(folder, load_config(folder / "config.json"))
    assert not report.blocking_issues  # warnings only: the analyst acknowledges or resolves them
