"""Real-world exports: file formats, CSV dialects, mapping suggestion, draft configs, profiles."""

from pathlib import Path

import pandas as pd
import pytest

from recon.config import FeedSpec, RunConfig
from recon.pipeline import MappingProfile, draft_config, inspect, load_profile, run, save_profile
from recon.schema import LINEAGE_COLUMNS

from test_pipeline import BANK, FIXTURE_ACKNOWLEDGEMENTS, MAPPING, OMS, POLICY, SHIP


def _write(folder: Path, stem: str, text: str, fmt: str) -> str:
    """Write a CSV fixture as `fmt`, typed the way a spreadsheet/warehouse would store it."""
    csv_path = folder / f"{stem}.csv"
    csv_path.write_text(text, encoding="utf-8")
    if fmt == "csv":
        return csv_path.name
    frame = pd.read_csv(csv_path)
    csv_path.unlink()
    if fmt == "xlsx":
        frame.to_excel(folder / f"{stem}.xlsx", index=False)
    else:
        frame.to_parquet(folder / f"{stem}.parquet", index=False)
    return f"{stem}.{fmt}"


def _config(folder: Path, fmt: str) -> RunConfig:
    feeds = {
        name: FeedSpec(_write(folder, name, text, fmt), format=fmt)
        for name, text in {"oms": OMS, "bank": BANK, "logistics": SHIP}.items()
    }
    return RunConfig(feeds=feeds, mapping=MAPPING, policy=POLICY, resolutions=FIXTURE_ACKNOWLEDGEMENTS)


def _canonical(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Drop lineage that legitimately differs per file (name, raw-row hash, load time)."""
    differs = [c for c in LINEAGE_COLUMNS if c != "source_row"]
    return {name: frame.drop(columns=differs) for name, frame in frames.items()}


@pytest.mark.parametrize("fmt", ["xlsx", "parquet"])
def test_same_data_loads_identically_from_csv_xlsx_parquet(tmp_path, fmt):
    (tmp_path / "csv").mkdir()
    (tmp_path / fmt).mkdir()
    from_csv = run(tmp_path / "csv", _config(tmp_path / "csv", "csv"))
    other = run(tmp_path / fmt, _config(tmp_path / fmt, fmt))

    assert other.health.gate_open
    for name, frame in _canonical(from_csv.feeds).items():
        pd.testing.assert_frame_equal(_canonical(other.feeds)[name], frame, check_dtype=False)
    cols = ["unit_id", "decision", "reason_codes"]
    pd.testing.assert_frame_equal(other.results[cols], from_csv.results[cols])


def test_semicolon_cp1252_csv_loads_without_declaring_its_dialect(tmp_path):
    config = _config(tmp_path, "csv")
    bank = BANK.replace("PAYMENT ORD2024-0000001", "PAGAMENTO JOÃO ORD2024-0000001").replace(",", ";")
    (tmp_path / config.feeds["bank"].path).write_bytes(bank.encode("cp1252"))

    result = run(tmp_path, config)

    assert result.health.gate_open
    bank_feed = result.feeds["bank"]
    assert bank_feed["description"].iloc[0] == "PAGAMENTO JOÃO ORD2024-0000001"
    assert bank_feed["credit_amount"].tolist() == [100.0, 55.0]


# Olist-shaped exports whose columns a different source system renamed.
H1, H2 = "2e7a8482f6fb09756ca50c10d7bfc047", "e5fa5a7210941f7d56d0208e4e071d35"
RENAMED_OMS = f"""Order Ref,Cust,AWB,Pay Method,Ccy,Total (BRL),Status,Order Date
{H1},08c5351a6aca1c1589a38f244edeee9d,OLIST-{H1},PREPAID,BRL,136.23,shipped,2016-09-04T21:15:19
{H2},683c54fc24d40ee9f8a6fc179fd9856c,OLIST-{H2},COD,BRL,75.06,canceled,2016-09-05T00:15:34
"""
RENAMED_BANK = f"""Bank Ref,Txn Dt,Value Dt,Narrative,Ccy,Credit,Charges,Net Settled
OLIST-PAY-{H1},2016-09-04T21:15:19,2016-10-07,OLIST PAYMENT {H1},BRL,136.23,2.45,133.78
OLIST-PAY-{H2},2016-09-05T00:15:34,2016-10-07,OLIST PAYMENT {H2},BRL,75.06,1.35,73.71
"""
RENAMED_SHIP = f"""Waybill,Merchant Order,State,Ccy,COD,Freight,COD Fee,Remittance,Picked
OLIST-{H1},{H1},SHIPPED,BRL,0.0,63.34,0.0,OLIST-SHIP-{H1},2016-10-18T13:14:51
OLIST-{H2},{H2},DELIVERED,BRL,75.06,8.49,0.6,OLIST-SHIP-{H2},2016-11-07T17:11:53
"""
RENAMED_FILES = {"oms": "orders.csv", "bank": "bank.csv", "logistics": "3pl.csv"}


def _write_renamed(folder: Path, bank: str = RENAMED_BANK) -> None:
    for name, text in zip(RENAMED_FILES.values(), (RENAMED_OMS, bank, RENAMED_SHIP)):
        (folder / name).write_text(text, encoding="utf-8")


def test_draft_config_suggests_the_mapping_for_renamed_columns(tmp_path):
    _write_renamed(tmp_path)

    draft = draft_config(tmp_path, RENAMED_FILES)

    assert draft.flagged == []
    assert draft.columns["bank"] == ("Bank Ref", "Txn Dt", "Value Dt", "Narrative", "Ccy",
                                     "Credit", "Charges", "Net Settled")
    assert draft.config.mapping == {
        "oms": {"order_id": "Order Ref", "tracking_no": "AWB", "payment_method": "Pay Method",
                "order_amount": "Total (BRL)", "ordered_at": "Order Date", "currency": "Ccy"},
        "bank": {"txn_ref": "Bank Ref", "description": "Narrative", "credit_amount": "Credit",
                 "fee_amount": "Charges", "net_amount": "Net Settled", "value_date": "Value Dt",
                 "currency": "Ccy"},
        "logistics": {"tracking_code": "Waybill", "client_order_code": "Merchant Order",
                      "cod_collected": "COD", "ship_fee": "Freight", "cod_fee": "COD Fee", "currency": "Ccy"},
    }
    assert inspect(tmp_path, draft.config).gate_open


def test_ambiguous_columns_are_flagged_and_never_applied(tmp_path):
    bank = RENAMED_BANK.replace("Credit,Charges,Net Settled", "Amt1,Amt2,Amt3")
    _write_renamed(tmp_path, bank=bank)

    draft = draft_config(tmp_path, RENAMED_FILES)

    money = {"credit_amount", "fee_amount", "net_amount"}
    assert {(s.feed, s.canonical) for s in draft.flagged} == {("bank", c) for c in money}
    assert draft.config.mapping["bank"] == {"txn_ref": "Bank Ref", "description": "Narrative",
                                            "value_date": "Value Dt", "currency": "Ccy"}
    report = inspect(tmp_path, draft.config)
    assert {i.id for i in report.blocking_issues} == {f"missing_column:bank:{c}" for c in money}
    remap = report.blocking_issues[0].fix_options[0]
    assert {"Amt1", "Amt2", "Amt3"} <= set(remap.params["candidates"])


OLIST = Path(__file__).resolve().parents[1] / "data" / "generated"


@pytest.mark.skipif(not (OLIST / "oms_orders.csv").exists(), reason="run recon.olistdata first")
def test_draft_config_from_olist_benchmark_passes_inspect():
    files = {"oms": "oms_orders.csv", "bank": "bank_statement.csv", "logistics": "logistics_report.csv"}

    draft = draft_config(OLIST, files)

    assert draft.flagged == []
    report = inspect(OLIST, draft.config)
    assert [i.id for i in report.issues if i.id.startswith("missing_column")] == []


def test_saved_profile_reloads_and_produces_the_same_mapping(tmp_path):
    _write_renamed(tmp_path, bank=RENAMED_BANK.replace("Credit,Charges,Net Settled", "Amt1,Amt2,Amt3"))
    draft = draft_config(tmp_path, RENAMED_FILES)
    confirmed = {**draft.config.mapping["bank"],
                 "credit_amount": "Amt1", "fee_amount": "Amt2", "net_amount": "Amt3"}
    save_profile(MappingProfile("acme-bank", "bank", confirmed), tmp_path / "acme-bank.json")

    profile = load_profile(tmp_path / "acme-bank.json")
    redraft = draft_config(tmp_path, RENAMED_FILES, profiles=[profile])

    assert profile.mapping == confirmed
    assert redraft.config.mapping["bank"] == confirmed
    assert redraft.flagged == []
    assert inspect(tmp_path, redraft.config).gate_open


def test_optional_currency_column_is_suggested_but_never_flagged(tmp_path):
    _write_renamed(tmp_path)
    draft = draft_config(tmp_path, RENAMED_FILES)
    assert {feed: cols.get("currency") for feed, cols in draft.config.mapping.items()} == \
        {"oms": "Ccy", "bank": "Ccy", "logistics": "Ccy"}

    no_ccy = RENAMED_BANK.replace("Narrative,Ccy,", "Narrative,").replace(",BRL,", ",")
    _write_renamed(tmp_path, bank=no_ccy)
    draft = draft_config(tmp_path, RENAMED_FILES)
    assert "currency" not in draft.config.mapping["bank"]
    assert draft.flagged == []
