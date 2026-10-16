"""Currencies compared like for like; mismatches surface and are fixed without dropping money."""

from dataclasses import replace

from recon.config import Resolution
from recon.pipeline import inspect, run

from test_pipeline import acknowledged, write_sources

# Order 1 is BRL, order 2 is USD; each bank credit is in its own order's currency.
OMS = """OrderNo,Tracking,Method,Amount,Placed,Status,currency
ORD2024-0000001,T1,PREPAID,100.00,2024-01-01T10:00:00,delivered,BRL
ORD2024-0000002,T2,PREPAID,55.00,2024-01-02T10:00:00,delivered,USD
"""
BANK = """Ref,Memo,currency,Gross,Fee,Net,ValueDate
P1,PAYMENT ORD2024-0000001,BRL,100.00,0,100.00,2024-01-02
P2,PAYMENT ORD2024-0000002,USD,55.00,0,55.00,2024-01-03
"""
SHIP = """Code,OrderRef,COD,ShipFee,CODFee
T1,ORD2024-0000001,0,5.0,0
T2,ORD2024-0000002,0,5.0,0
"""
# The same credit for order 2 arrives labelled BRL: a currency code that disagrees with its order.
MISLABELLED_BANK = BANK.replace("P2,PAYMENT ORD2024-0000002,USD", "P2,PAYMENT ORD2024-0000002,BRL")


def sources(tmp_path, bank=BANK):
    config = write_sources(tmp_path, oms=OMS, bank=bank, ship=SHIP)
    return replace(config, resolutions=())


def by_order(outcome):
    return outcome.results.set_index("order_id")


def mismatch_issue(report):
    [issue] = [i for i in report.issues if i.check == "currency_mismatch"]
    return issue


def resolve(config, issue, option_id, note="", **overrides):
    [option] = [o for o in issue.fix_options if o.id == option_id and all(o.params.get(k) == v for k, v in overrides.items())]
    return replace(config, resolutions=config.resolutions + (Resolution(issue.id, option.id, option.params, note),))


def test_feed_with_several_currencies_is_not_blocking_and_ties_out_per_currency(tmp_path):
    config = sources(tmp_path)

    report = inspect(tmp_path, config)

    assert report.gate_open
    assert report.profiles["currency_mix"]["bank"] == {"BRL": 1, "USD": 1}
    outcome = run(tmp_path, config)
    assert set(by_order(outcome)["decision"]) == {"AUTO_ACCEPT"}


def test_credit_in_another_currency_than_its_order_is_a_currency_mismatch(tmp_path):
    config = sources(tmp_path, bank=MISLABELLED_BANK)

    issue = mismatch_issue(inspect(tmp_path, config))

    assert issue.id == "currency_mismatch:bank"
    assert issue.affected_rows == 1
    assert [row["source_row"] for row in issue.sample_rows] == [2]
    outcome = run(tmp_path, acknowledged(tmp_path, config))
    unit = by_order(outcome).loc["ORD2024-0000002"]
    assert unit["decision"] == "EXCEPTION"
    assert unit["reason_codes"] == "CURRENCY_MISMATCH"
    assert "BRL" in unit["explanation"] and "USD" in unit["explanation"]


def test_correcting_the_currency_code_compares_normally_and_leaves_the_file_untouched(tmp_path):
    config = sources(tmp_path, bank=MISLABELLED_BANK)
    raw = (tmp_path / config.feeds["bank"].path).read_bytes()
    issue = mismatch_issue(inspect(tmp_path, config))

    fixed = resolve(config, issue, "correct_currency", note="bank labels USD credits as BRL", currency="USD")

    assert inspect(tmp_path, fixed).gate_open
    outcome = run(tmp_path, fixed)
    assert by_order(outcome).loc["ORD2024-0000002", "decision"] == "AUTO_ACCEPT"
    assert (tmp_path / config.feeds["bank"].path).read_bytes() == raw


def test_routing_currency_mismatches_to_review(tmp_path):
    config = sources(tmp_path, bank=MISLABELLED_BANK)
    issue = mismatch_issue(inspect(tmp_path, config))

    routed = resolve(config, issue, "route_to_review", note="treasury checks these")

    assert inspect(tmp_path, routed).gate_open
    unit = by_order(run(tmp_path, routed)).loc["ORD2024-0000002"]
    assert unit["decision"] == "REVIEW"
    assert unit["reason_codes"] == "CURRENCY_MISMATCH"


def test_excluding_is_still_offered_as_a_last_resort(tmp_path):
    config = sources(tmp_path, bank=MISLABELLED_BANK)
    issue = mismatch_issue(inspect(tmp_path, config))

    assert [o.id for o in issue.fix_options][-2:] == ["exclude_rows", "acknowledge"]


def test_fees_are_computed_in_the_currency_minor_unit(tmp_path):
    oms = OMS.replace("100.00,2024", "99.90,2024").replace(",55.00,2024-01-02T10:00:00,delivered,USD",
                                                           ",430000,2024-01-02T10:00:00,delivered,VND")
    bank = BANK.replace("P1,PAYMENT ORD2024-0000001,BRL,100.00,0,100.00", "P1,PAYMENT ORD2024-0000001,BRL,99.90,3.99,95.91") \
        .replace("P2,PAYMENT ORD2024-0000002,USD,55.00,0,55.00", "P2,PAYMENT ORD2024-0000002,VND,430000,17157,412843")
    config = replace(write_sources(tmp_path, oms=oms, bank=bank, ship=SHIP), resolutions=())
    config = replace(config, policy=replace(config.policy, mdr_bps=399))

    units = by_order(run(tmp_path, acknowledged(tmp_path, config)))

    assert units.loc["ORD2024-0000001", "expected_mdr"] == 3.99  # not 4, from the truncated 99
    assert units.loc["ORD2024-0000002", "expected_mdr"] == 17_157  # VND has no minor unit
    assert set(units["decision"]) == {"AUTO_ACCEPT"}
