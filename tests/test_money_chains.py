"""Named feeds with a role, channels and the order reference per money chain (ADR 0001)."""

from dataclasses import replace
from pathlib import Path

import pytest

from recon.config import Channel, FeedSpec, Policy, RunConfig
from recon.pipeline import inspect, run

from test_funnel_order import BANK_HEADER, SHIP_HEADER, shipment
from test_pipeline import acknowledged

OMS_HEADER = "order_id,tracking_no,payment_method,order_amount,ordered_at,order_status,channel\n"
SETTLEMENT = ("order_no,payout_id,gross,net,settled_at\n"
              "SHP-1,PO-1,100.00,90.00,2024-01-05\n"
              "SHP-1,PO-1,100.00,90.00,2024-01-05\n"
              "SHP-2,PO-1,abc,45.00,2024-01-05\n")
FEEDS = {"oms": FeedSpec("oms.csv"), "bank": FeedSpec("bank.csv"), "logistics": FeedSpec("ship.csv")}


def write(folder: Path, orders: str, bank: str, ship: str) -> None:
    (folder / "oms.csv").write_text(OMS_HEADER + orders, encoding="utf-8")
    (folder / "bank.csv").write_text(BANK_HEADER + bank, encoding="utf-8")
    (folder / "ship.csv").write_text(SHIP_HEADER + ship, encoding="utf-8")


def haravan(folder: Path) -> RunConfig:
    """One website order with a Haravan-style code, paid by a transfer that names it."""
    write(folder, "#1001,T1,PREPAID,100.00,2024-01-01T10:00:00,delivered,website\n",
          "P1,CK THANH TOAN DON #1001,100.00,0,100.00,2024-01-09\n", "T1,#1001,0,5.0,0,,\n")
    return RunConfig(feeds=FEEDS, policy=Policy(mdr_bps=0, cod_fee_bps=0, link_date_window_days=1))


def test_a_settlement_report_loads_as_a_fourth_feed_and_its_health_checks_run(tmp_path):
    config = haravan(tmp_path)
    (tmp_path / "shopee.csv").write_text(SETTLEMENT, encoding="utf-8")
    config = replace(config, feeds={**FEEDS, "shopee": FeedSpec("shopee.csv", role="settlement")})

    issues = {issue.id: issue for issue in inspect(tmp_path, config).issues}

    assert issues["duplicate_key:shopee:order_no"].affected_rows == 2
    assert issues["unparseable_value:shopee:gross"].affected_rows == 1
    assert "shopee.net" in inspect(tmp_path, config).profiles["amount_distribution"]


def test_a_settlement_report_missing_a_required_column_blocks(tmp_path):
    config = haravan(tmp_path)
    (tmp_path / "shopee.csv").write_text("order_no,gross,net\nSHP-1,100,90\n", encoding="utf-8")
    config = replace(config, feeds={**FEEDS, "shopee": FeedSpec("shopee.csv", role="settlement")})

    blocking = [issue.id for issue in inspect(tmp_path, config).blocking_issues]

    assert blocking == ["missing_column:shopee:payout_id"]


def test_a_feed_needs_a_role_that_fits_its_name():
    with pytest.raises(ValueError, match="needs a role"):
        RunConfig(feeds={**FEEDS, "shopee": FeedSpec("shopee.csv")})
    with pytest.raises(ValueError, match="has the bank role"):
        RunConfig(feeds={**FEEDS, "bank": FeedSpec("bank.csv", role="settlement")})
    with pytest.raises(ValueError, match="unknown feed role"):
        FeedSpec("x.csv", role="ledger")


def test_a_haravan_order_code_links_in_pass_one_when_the_chain_sets_its_pattern(tmp_path):
    config = haravan(tmp_path)
    default = run(tmp_path, acknowledged(tmp_path, config)).results.set_index("order_id").loc["#1001"]
    chained = replace(config, channels={"website": Channel(order_ref_pattern=r"DON (?P<ref>#\d+)")})
    linked = run(tmp_path, acknowledged(tmp_path, chained)).results.set_index("order_id").loc["#1001"]

    assert default["bank_link_pass"] != "exact" and default["decision"] != "AUTO_ACCEPT"
    assert (linked["bank_link_pass"], linked["txn_ref"], linked["decision"]) == ("exact", "P1", "AUTO_ACCEPT")


def test_orders_of_an_undeclared_channel_are_a_warning(tmp_path):
    config = replace(haravan(tmp_path), channels={"shopee": Channel()})

    warned = [issue for issue in inspect(tmp_path, config).issues if issue.check == "channel_not_configured"]

    assert [(issue.id, issue.affected_rows) for issue in warned] == [("channel_not_configured:website", 1)]
    assert not [issue for issue in inspect(tmp_path, haravan(tmp_path)).issues
                if issue.check == "channel_not_configured"]


def test_roles_and_channels_survive_the_config_round_trip():
    config = RunConfig(feeds={**FEEDS, "shopee": FeedSpec("shopee.csv", role="settlement")},
                       channels={"website": Channel(order_ref_pattern=r"#\d+")})

    assert RunConfig.from_dict(config.to_dict()) == config
    with pytest.raises(ValueError, match="unknown money chain"):
        Channel(chain="barter")
