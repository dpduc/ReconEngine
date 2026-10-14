"""FX conversion: a recorded rate source, an FX tolerance, and FX differences kept apart from amount errors."""

import hashlib
from dataclasses import replace

import pytest

from recon.config import Resolution
from recon.pipeline import inspect, run

from test_pipeline import write_sources

SHIP = """Code,OrderRef,COD,ShipFee,CODFee
T1,ORD2024-0000001,0,5.0,0
T2,ORD2024-0000002,0,5.0,0
"""
# Order 2 is in BRL; its credit arrives in USD (55.00) with value date 2024-01-03.
BANK = """Ref,Memo,currency,Gross,Fee,Net,ValueDate
P1,PAYMENT ORD2024-0000001,BRL,100.00,0,100.00,2024-01-02
P2,PAYMENT ORD2024-0000002,USD,55.00,0,55.00,2024-01-03
"""
RATES = """date,from,to,rate
2024-01-01,USD,BRL,5.00
2024-01-05,USD,BRL,5.20
"""


def oms(order_2_amount: str) -> str:
    return ("OrderNo,Tracking,Method,Amount,Placed,Status,currency\n"
            "ORD2024-0000001,T1,PREPAID,100.00,2024-01-01T10:00:00,delivered,BRL\n"
            f"ORD2024-0000002,T2,PREPAID,{order_2_amount},2024-01-02T10:00:00,delivered,BRL\n")


def converted(tmp_path, order_2_amount="275.00", bank=BANK, rates=RATES, **fx):
    """Sources with the currency mismatch resolved by 'convert using a rate source'."""
    config = replace(write_sources(tmp_path, oms=oms(order_2_amount), bank=bank, ship=SHIP), resolutions=())
    if rates is not None:
        (tmp_path / "fx.csv").write_text(rates, encoding="utf-8")
    [issue] = [i for i in inspect(tmp_path, config).issues if i.check == "currency_mismatch"]
    [option] = [o for o in issue.fix_options if o.id == "convert_currency"]
    params = {**option.params, "rate_table": "fx.csv" if rates is not None else None, **fx}
    return replace(config, resolutions=(Resolution(issue.id, option.id, params, "treasury rates"),))


def unit(outcome):
    return outcome.results.set_index("order_id").loc["ORD2024-0000002"]


def test_rate_table_converts_at_the_rate_on_or_before_the_value_date(tmp_path):
    config = converted(tmp_path)

    assert inspect(tmp_path, config).gate_open
    row = unit(run(tmp_path, config))
    assert row["decision"] == "AUTO_ACCEPT"
    assert row["fx_rate"] == pytest.approx(5.00)
    assert row["fx_rate_date"] == "2024-01-01"
    assert row["fx_rate_source"] == "rate table fx.csv"
    assert "5" in row["explanation"] and "fx.csv" in row["explanation"]


def test_small_fx_difference_is_fx_difference_and_large_one_is_amount_mismatch(tmp_path):
    small = unit(run(tmp_path, converted(tmp_path, order_2_amount="276.65")))  # 0.6% off
    assert (small["decision"], small["reason_codes"]) == ("EXCEPTION", "FX_DIFFERENCE")
    assert small["priority"] == 1

    large = unit(run(tmp_path, converted(tmp_path, order_2_amount="289.50")))  # 5% off
    assert large["reason_codes"] == "AMOUNT_MISMATCH"


def test_bank_rate_column_is_preferred_over_the_table(tmp_path):
    bank = BANK.replace("Net,ValueDate", "Net,ValueDate,fx_rate").replace(",2024-01-02\n", ",2024-01-02,\n") \
               .replace(",2024-01-03\n", ",2024-01-03,5.10\n")
    row = unit(run(tmp_path, converted(tmp_path, order_2_amount="280.50", bank=bank, rate_column="fx_rate")))

    assert row["decision"] == "AUTO_ACCEPT"
    assert row["fx_rate"] == pytest.approx(5.10)
    assert row["fx_rate_source"] == "bank column fx_rate"


def test_missing_rate_for_the_date_is_a_currency_mismatch_saying_so(tmp_path):
    late_rates = "date,from,to,rate\n2024-01-05,USD,BRL,5.20\n"
    row = unit(run(tmp_path, converted(tmp_path, rates=late_rates)))

    assert row["reason_codes"] == "CURRENCY_MISMATCH"
    assert "rate" in row["explanation"].lower()


def test_manifest_records_the_fx_setting_and_rate_table_hash(tmp_path):
    config = converted(tmp_path)

    first, second = run(tmp_path, config), run(tmp_path, config)

    files = first.manifest["input_files"]
    assert files["fx_rates"] == {"path": "fx.csv",
                                 "sha256": hashlib.sha256((tmp_path / "fx.csv").read_bytes()).hexdigest()}
    fx = first.manifest["run_config"]["resolutions"][0]["params"]
    assert fx["rate_table"] == "fx.csv" and fx["tolerance"] == 0.01
    assert first.results.drop(columns=[c for c in first.results if c.endswith("ingested_at")]).equals(
        second.results.drop(columns=[c for c in second.results if c.endswith("ingested_at")]))
