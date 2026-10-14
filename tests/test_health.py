"""Warning checks, acknowledgement, EDA profiles and Health Report exports via inspect / run."""

import html
import json
from pathlib import Path

import pandas as pd

from recon.config import FeedSpec, Policy, Resolution, RunConfig
from recon.pipeline import inspect, run


# A fully consistent set: every order is paid and shipped, timestamps carry real times.
OMS = """order_id,tracking_no,payment_method,order_amount,ordered_at,order_status
ORD2024-0000001,T1,PREPAID,100.00,2024-01-01T10:00:00,delivered
ORD2024-0000002,T2,PREPAID,50.00,2024-01-02T11:30:00,delivered
ORD2024-0000003,T3,PREPAID,70.00,2024-01-03T09:15:00,shipped
ORD2024-0000004,T4,PREPAID,80.00,2024-01-04T14:45:00,delivered
"""
BANK = """txn_ref,description,credit_amount,fee_amount,net_amount,txn_date,value_date
P1,PAYMENT ORD2024-0000001,100.00,0,100.00,2024-01-01T10:05:00,2024-01-02
P2,PAYMENT ORD2024-0000002,50.00,0,50.00,2024-01-02T11:35:00,2024-01-03
P3,PAYMENT ORD2024-0000003,70.00,0,70.00,2024-01-03T09:20:00,2024-01-04
P4,PAYMENT ORD2024-0000004,80.00,0,80.00,2024-01-04T14:50:00,2024-01-05
"""
SHIP = """tracking_code,client_order_code,cod_collected,ship_fee,cod_fee,status,picked_at,delivered_at
T1,ORD2024-0000001,0,5.0,0,DELIVERED,2024-01-02T08:10:00,2024-01-03T15:20:00
T2,ORD2024-0000002,0,5.0,0,DELIVERED,2024-01-03T09:40:00,2024-01-04T16:05:00
T3,ORD2024-0000003,0,5.0,0,SHIPPED,2024-01-04T07:55:00,
T4,ORD2024-0000004,0,5.0,0,DELIVERED,2024-01-05T10:30:00,2024-01-06T12:45:00
"""
POLICY = Policy(mdr_bps=0, cod_fee_bps=0, amount_tolerance=0.01)


def write_sources(folder: Path, oms: str = OMS, bank: str = BANK, ship: str = SHIP,
                  resolutions: tuple[Resolution, ...] = ()) -> RunConfig:
    (folder / "oms.csv").write_text(oms, encoding="utf-8")
    (folder / "bank.csv").write_text(bank, encoding="utf-8")
    (folder / "ship.csv").write_text(ship, encoding="utf-8")
    return RunConfig(
        feeds={"oms": FeedSpec("oms.csv"), "bank": FeedSpec("bank.csv"), "logistics": FeedSpec("ship.csv")},
        mapping={},
        policy=POLICY,
        resolutions=resolutions,
    )


def warnings_of(report, check):
    return {issue.id: issue for issue in report.issues
            if issue.check == check and issue.severity.value == "warning"}


def test_clean_sources_raise_no_warnings(tmp_path):
    report = inspect(tmp_path, write_sources(tmp_path))
    assert [issue.id for issue in report.issues] == []
    assert report.gate_open


def test_missing_monetary_value_is_a_warning(tmp_path):
    oms = OMS.replace("50.00,2024", ",2024").replace("70.00,2024", ",2024")
    report = inspect(tmp_path, write_sources(tmp_path, oms=oms))
    issue = warnings_of(report, "missing_amount")["missing_amount:oms:order_amount"]
    assert issue.feed == "oms"
    assert issue.affected_rows == 2
    assert not report.gate_open


def test_zero_credit_is_a_warning(tmp_path):
    bank = BANK.replace("70.00,0,70.00", "0,0,0")
    report = inspect(tmp_path, write_sources(tmp_path, bank=bank))
    assert warnings_of(report, "zero_amount")["zero_amount:bank:credit_amount"].affected_rows == 1


def test_coverage_gaps_are_warnings(tmp_path):
    oms = OMS + "ORD2024-0000005,T5,PREPAID,90.00,2024-01-05T10:00:00,delivered\n"
    bank = BANK + "P9,PAYMENT ORD2024-0000099,10.00,0,10.00,2024-01-05T10:00:00,2024-01-06\n"
    ship = SHIP + ("T8,ORD2024-0000088,0,5.0,0,DELIVERED,2024-01-05T10:00:00,2024-01-06T11:00:00\n"
                   "T9,ORD2024-0000099,0,5.0,0,DELIVERED,2024-01-05T10:00:00,2024-01-06T11:00:00\n")
    report = inspect(tmp_path, write_sources(tmp_path, oms=oms, bank=bank, ship=ship))
    counts = {issue_id: issue.affected_rows for issue_id, issue in warnings_of(report, "coverage").items()}
    assert counts == {
        "coverage:oms:no_payment": 1,
        "coverage:oms:no_shipment": 1,
        "coverage:bank:no_order": 1,
        "coverage:logistics:no_order": 2,
    }


def test_run_is_blocked_until_every_warning_is_acknowledged(tmp_path):
    oms = OMS.replace("50.00,2024", ",2024")
    bank = BANK.replace("70.00,0,70.00", "0,0,0")
    config = write_sources(tmp_path, oms=oms, bank=bank)
    outcome = run(tmp_path, config)
    assert outcome.blocked
    assert {i.id for i in outcome.open_issues} == {"missing_amount:oms:order_amount", "zero_amount:bank:credit_amount"}

    ack = lambda issue_id, note="Known source gap": Resolution(issue_id, "acknowledge", note=note)
    partly = write_sources(tmp_path, oms=oms, bank=bank, resolutions=(ack("missing_amount:oms:order_amount"),))
    assert [i.id for i in run(tmp_path, partly).open_issues] == ["zero_amount:bank:credit_amount"]

    without_note = write_sources(tmp_path, oms=oms, bank=bank, resolutions=(
        ack("missing_amount:oms:order_amount"), ack("zero_amount:bank:credit_amount", note="  ")))
    assert run(tmp_path, without_note).blocked

    full = write_sources(tmp_path, oms=oms, bank=bank, resolutions=(
        ack("missing_amount:oms:order_amount"), ack("zero_amount:bank:credit_amount")))
    outcome = run(tmp_path, full)
    assert not outcome.blocked
    assert len(outcome.results) == 4
    assert all(issue["acknowledged"] for issue in outcome.health.to_dict()["issues"])


def test_acknowledging_cannot_open_a_blocking_issue(tmp_path):
    oms = OMS.replace("order_amount", "total")
    config = write_sources(tmp_path, oms=oms, resolutions=(
        Resolution("missing_column:oms:order_amount", "acknowledge", note="ignore"),))
    assert run(tmp_path, config).blocked


def test_value_date_lag_profile_reports_percentiles(tmp_path):
    # 101 payments booked at 10:00 on Jan 1 and valued 0, 1, ..., 100 days later.
    start = pd.Timestamp("2024-01-01T10:00:00")
    rows = [f"P{k},PAYMENT ORD2024-{k:07d},10.00,0,10.00,{start.isoformat()},"
            f"{(start + pd.Timedelta(days=k)).date().isoformat()}" for k in range(101)]
    bank = BANK.splitlines()[0] + "\n" + "\n".join(rows) + "\n"
    lag = inspect(tmp_path, write_sources(tmp_path, bank=bank)).profiles["value_date_lag_days"]
    assert (lag["p50"], lag["p95"], lag["p99"]) == (50, 95, 99)
    assert lag["max"] == 100 and lag["count"] == 101


def test_profiles_describe_amounts_and_status_mix(tmp_path):
    profiles = inspect(tmp_path, write_sources(tmp_path)).profiles
    assert profiles["order_status_mix"] == {"delivered": 3, "shipped": 1}
    amounts = profiles["amount_distribution"]["oms.order_amount"]
    assert (amounts["count"], amounts["min"], amounts["max"], amounts["mean"], amounts["p50"]) == (4, 50, 100, 75, 75)


def test_report_exports_every_issue_and_profile_as_json_and_html(tmp_path):
    oms = OMS.replace("order_status", "Status") + \
        "ORD2024-0000005,T5,PREPAID,10000.00,2024-01-05T10:00:00,delivered\n" + \
        "ORD2024-0000006,T6,PREPAID,,2024-01-06T10:00:00,delivered\n"
    ship = SHIP.replace("2024-01-03T09:40:00", "2024-01-04T18:00:00").replace("cod_fee,", "")
    ship = "\n".join(line.replace(",0,DELIVERED", ",DELIVERED").replace(",0,SHIPPED", ",SHIPPED")
                     for line in ship.splitlines()) + "\n"
    config = write_sources(tmp_path, oms=oms, ship=ship, resolutions=(
        Resolution("missing_amount:oms:order_amount", "acknowledge", note="OMS has no items for <this> order"),))
    report = inspect(tmp_path, config)
    checks = {issue.check for issue in report.issues}
    assert {"missing_required_column", "missing_amount", "coverage", "amount_outlier",
            "picked_after_delivered"} <= checks
    assert set(report.profiles) == {"amount_distribution", "value_date_lag_days"}

    exported = json.loads(report.to_json())
    assert [i["id"] for i in exported["issues"]] == [i.id for i in report.issues]
    assert exported["profiles"] == json.loads(json.dumps(report.profiles))
    assert exported["gate_open"] is False

    page = report.to_html()
    assert page.lstrip().lower().startswith("<!doctype html>")
    assert "<script" not in page and "http" not in page  # standalone: nothing to fetch
    for issue in report.issues:
        assert issue.id in page and html.escape(issue.message) in page
    for name, value in report.profiles.items():
        assert name in page
    assert "p99" in page and "oms.order_amount" in page
    assert "OMS has no items for &lt;this&gt; order" in page  # acknowledgement note, escaped


def test_amount_outlier_is_a_warning(tmp_path):
    oms = OMS + "ORD2024-0000005,T5,PREPAID,10000.00,2024-01-05T10:00:00,delivered\n"
    report = inspect(tmp_path, write_sources(tmp_path, oms=oms))
    found = warnings_of(report, "amount_outlier")
    assert {k: v.affected_rows for k, v in found.items()} == {"amount_outlier:oms:order_amount": 1}
    assert found["amount_outlier:oms:order_amount"].sample_rows[0]["order_id"] == "ORD2024-0000005"


def test_picked_after_delivered_is_a_warning(tmp_path):
    ship = SHIP.replace("2024-01-03T09:40:00,2024-01-04T16:05:00", "2024-01-04T18:00:00,2024-01-04T16:05:00")
    report = inspect(tmp_path, write_sources(tmp_path, ship=ship))
    issue = warnings_of(report, "picked_after_delivered")["picked_after_delivered:logistics"]
    assert issue.affected_rows == 1


def test_placeholder_timestamps_in_a_segment_are_a_warning(tmp_path):
    # Every SHIPPED row carries a midnight delivered_at; DELIVERED rows have real times.
    ship = SHIP.replace("SHIPPED,2024-01-04T07:55:00,", "SHIPPED,2024-01-04T07:55:00,2024-01-05T00:00:00")
    ship += "T5,ORD2024-0000005,0,5.0,0,SHIPPED,2024-01-05T07:55:00,2024-01-06T00:00:00\n"
    report = inspect(tmp_path, write_sources(tmp_path, ship=ship))
    found = warnings_of(report, "placeholder_timestamp")
    assert list(found) == ["placeholder_timestamp:logistics:delivered_at:status=SHIPPED"]
    assert found["placeholder_timestamp:logistics:delivered_at:status=SHIPPED"].affected_rows == 2


def test_placeholder_timestamps_in_a_whole_column_are_a_warning(tmp_path):
    oms = "\n".join(line[:line.rfind("T")] + "T00:00:00," + line.rsplit(",", 1)[1] if i else line
                    for i, line in enumerate(OMS.splitlines())) + "\n"
    report = inspect(tmp_path, write_sources(tmp_path, oms=oms))
    found = warnings_of(report, "placeholder_timestamp")
    assert {k: v.affected_rows for k, v in found.items()} == {"placeholder_timestamp:oms:ordered_at": 4}


def test_statuses_outside_the_policy_are_warnings_fixed_by_setting_their_evidence(tmp_path):
    from recon.pipeline import resolve

    oms = OMS.replace("70.00,2024-01-03T09:15:00,shipped", "70.00,2024-01-03T09:15:00,canceled")
    ship = SHIP.replace("T3,ORD2024-0000003,0,5.0,0,SHIPPED,2024-01-04T07:55:00,\n", "")
    config = write_sources(tmp_path, oms=oms, ship=ship,
                           resolutions=(Resolution("coverage:oms:no_shipment", "acknowledge", note="canceled"),))
    report = inspect(tmp_path, config)
    issue = warnings_of(report, "status_not_in_policy")["status_not_in_policy:canceled"]
    assert issue.affected_rows == 1
    assert not report.gate_open
    assert "acknowledge" in {option.id for option in issue.fix_options}
    payment_only = next(o for o in issue.fix_options if o.params.get("evidence") == ["payment"])

    resolved = resolve(config, issue.id, payment_only.id, payment_only.params)
    assert resolved.policy == config.policy  # the policy change lives in the resolution, replayably
    report = inspect(tmp_path, resolved)
    assert "status_not_in_policy:canceled" not in {i.id for i in report.issues}
    assert report.gate_open
    results = run(tmp_path, resolved).results.set_index("order_id")
    assert results.loc["ORD2024-0000003", "decision"] == "AUTO_ACCEPT"
