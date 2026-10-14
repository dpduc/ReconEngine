"""Blocking integrity checks and their resolutions, through inspect / run."""

from dataclasses import replace

import pandas as pd

from recon.config import Resolution, RunConfig
from recon.pipeline import inspect, run

from test_pipeline import BANK, OMS, SHIP, acknowledged, write_sources

DUP_OMS = OMS + "ORD2024-0000001,T1,PREPAID,100.00,2024-01-05T10:00:00,delivered\n"


def resolve(config, issue, option, note="", **params):
    resolution = Resolution(issue.id, option, params, note)
    return replace(config, resolutions=config.resolutions + (resolution,))


def only_issue(report, check):
    [issue] = [i for i in report.blocking_issues if i.check == check]
    return issue


def test_duplicate_primary_key_blocks_run(tmp_path):
    config = write_sources(tmp_path, oms=DUP_OMS)
    issue = only_issue(inspect(tmp_path, config), "duplicate_key")
    assert issue.feed == "oms"
    assert issue.affected_rows == 2
    assert {row["source_row"] for row in issue.sample_rows} == {1, 4}

    outcome = run(tmp_path, config)
    assert outcome.blocked
    assert issue.id in [i.id for i in outcome.open_issues]


def test_keep_first_resolves_duplicates_and_reports_the_rest_as_excluded(tmp_path):
    config = write_sources(tmp_path, oms=DUP_OMS)
    issue = only_issue(inspect(tmp_path, config), "duplicate_key")
    config = resolve(config, issue, "duplicate_strategy", note="re-export", feed="oms", strategy="keep_first")
    assert inspect(tmp_path, config).gate_open

    outcome = run(tmp_path, config)
    assert not outcome.blocked
    assert len(outcome.results) == 3
    [excluded] = outcome.excluded.to_dict("records")
    assert (excluded["feed"], excluded["source_row"], excluded["issue_id"]) == ("oms", 4, issue.id)
    assert "keep_first" in excluded["reason"]


def test_resolutions_replay_from_the_saved_config(tmp_path):
    config = write_sources(tmp_path, oms=DUP_OMS)
    issue = only_issue(inspect(tmp_path, config), "duplicate_key")
    config = resolve(config, issue, "duplicate_strategy", feed="oms", strategy="keep_first")
    first = run(tmp_path, config)
    replayed = run(tmp_path, RunConfig.from_dict(first.manifest["run_config"]))
    pd.testing.assert_frame_equal(first.excluded, replayed.excluded)
    pd.testing.assert_frame_equal(first.results, replayed.results)
    assert first.manifest["excluded_rows"] == {"oms": 1}


def test_keep_latest_keeps_the_most_recent_duplicate(tmp_path):
    # The later-dated copy sits first in the file, so "latest" is not file order.
    oms = OMS.replace("ORD2024-0000003,T3,PREPAID,70.00,2024-01-03",
                      "ORD2024-0000003,T3,PREPAID,70.00,2024-01-09")
    oms += "ORD2024-0000003,T3,PREPAID,70.00,2024-01-04T10:00:00,delivered\n"
    config = write_sources(tmp_path, oms=oms)
    issue = only_issue(inspect(tmp_path, config), "duplicate_key")
    config = resolve(config, issue, "duplicate_strategy", feed="oms", strategy="keep_latest")
    assert inspect(tmp_path, config).gate_open

    outcome = run(tmp_path, config)
    assert outcome.excluded["source_row"].tolist() == [4]
    kept = outcome.results.set_index("order_id").loc["ORD2024-0000003"]
    assert kept["ordered_at"].startswith("2024-01-09")


def test_route_to_review_sends_every_duplicate_unit_to_review(tmp_path):
    config = write_sources(tmp_path, oms=DUP_OMS)
    issue = only_issue(inspect(tmp_path, config), "duplicate_key")
    config = resolve(config, issue, "duplicate_strategy", feed="oms", strategy="review")
    assert inspect(tmp_path, config).gate_open

    outcome = run(tmp_path, config)
    assert outcome.excluded.empty
    dupes = outcome.results[outcome.results["order_id"] == "ORD2024-0000001"]
    assert len(dupes) == 2
    assert (dupes["decision"] == "REVIEW").all()
    assert dupes["reason_codes"].str.contains("DUPLICATE").all()


def test_route_to_review_covers_duplicate_bank_references(tmp_path):
    bank = BANK.replace("P2,", "P1,")
    config = write_sources(tmp_path, bank=bank)
    issue = only_issue(inspect(tmp_path, config), "duplicate_key")
    assert issue.feed == "bank"
    config = resolve(config, issue, "duplicate_strategy", feed="bank", strategy="review")

    results = run(tmp_path, config).results.set_index("order_id")
    assert results.loc[["ORD2024-0000001", "ORD2024-0000002"], "decision"].eq("REVIEW").all()
    assert results.loc["ORD2024-0000003", "decision"] == "EXCEPTION"


def test_unparseable_number_blocks_and_excluding_the_row_unblocks(tmp_path):
    config = write_sources(tmp_path, oms=OMS.replace("50.00", "5O.OO"))
    issue = only_issue(inspect(tmp_path, config), "unparseable_value")
    assert (issue.feed, issue.affected_rows) == ("oms", 1)
    assert issue.sample_rows[0]["order_amount"] == "5O.OO"
    assert run(tmp_path, config).blocked

    config = resolve(config, issue, "exclude_rows", feed="oms", source_rows=[2], reason="corrupt amount")
    # Excluding the order leaves its payment and shipment without an order: warnings to acknowledge.
    assert [i.id for i in inspect(tmp_path, config).open_issues] == [
        "coverage:bank:no_order", "coverage:logistics:no_order"]
    config = acknowledged(tmp_path, config)
    assert inspect(tmp_path, config).gate_open
    outcome = run(tmp_path, config)
    assert "ORD2024-0000002" not in set(outcome.results["order_id"])
    [excluded] = outcome.excluded.to_dict("records")
    assert (excluded["feed"], excluded["source_row"], excluded["reason"]) == ("oms", 2, "corrupt amount")
    assert "excluded.csv" in outcome.bundle()


DMY_BANK = """Ref,Memo,Gross,Fee,Net,ValueDate
P1,PAYMENT ORD2024-0000001,100.00,0,100.00,02/01/2024
P2,PAYMENT ORD2024-0000002,55.00,0,55.00,13/01/2024
"""


def test_unparseable_dates_block_and_choosing_a_format_unblocks(tmp_path):
    config = write_sources(tmp_path, bank=DMY_BANK)
    issue = only_issue(inspect(tmp_path, config), "unparseable_date")
    assert (issue.feed, issue.affected_rows) == ("bank", 2)
    [option] = [o for o in issue.fix_options if o.id == "set_date_format"]
    assert option.params["candidates"] == ["%d/%m/%Y"]
    assert run(tmp_path, config).blocked

    config = resolve(config, issue, "set_date_format", feed="bank", date_format="%d/%m/%Y")
    assert inspect(tmp_path, config).gate_open
    outcome = run(tmp_path, config)
    assert not outcome.blocked
    assert sorted(outcome.feeds["bank"]["value_date"]) == ["2024-01-02", "2024-01-13"]


CURRENCY_BANK = """Ref,Memo,currency,Gross,Fee,Net,ValueDate
P1,PAYMENT ORD2024-0000001,BRL,100.00,0,100.00,2024-01-02
P2,PAYMENT ORD2024-0000002,USD,55.00,0,55.00,2024-01-03
"""


def test_single_currency_feed_is_clean(tmp_path):
    config = write_sources(tmp_path, bank=CURRENCY_BANK.replace("USD", "BRL"))
    assert inspect(tmp_path, config).gate_open


def test_several_currencies_in_one_feed_do_not_block(tmp_path):
    # Like-for-like comparison per linked pair (see test_currency); the mix is only profiled.
    config = write_sources(tmp_path, bank=CURRENCY_BANK)
    report = inspect(tmp_path, config)
    assert not report.blocking_issues
    assert report.profiles["currency_mix"]["bank"] == {"BRL": 1, "USD": 1}


def test_feed_spec_date_format_is_honoured(tmp_path):
    config = write_sources(tmp_path, bank=DMY_BANK)
    feeds = {**config.feeds, "bank": replace(config.feeds["bank"], date_format="%d/%m/%Y")}
    assert inspect(tmp_path, replace(config, feeds=feeds)).gate_open
