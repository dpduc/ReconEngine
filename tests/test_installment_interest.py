"""Installment interest: auto-accepted with a note only when the statement reports it; else a reviewer confirms."""

import io

import pandas as pd

from recon.pipeline import run

from test_pipeline import write_sources

OMS = """OrderNo,Tracking,Method,Amount,Placed,Status
ORD2024-0000001,T1,PREPAID,100.00,2024-01-01T10:00:00,delivered
ORD2024-0000002,T2,PREPAID,200.00,2024-01-02T10:00:00,delivered
"""
SHIP = """Code,OrderRef,COD,ShipFee,CODFee
T1,ORD2024-0000001,0,5.0,0
T2,ORD2024-0000002,0,5.0,0
"""


def bank(credit: str, installments: str | None = "3", interest: str | None = "18.00") -> str:
    """Order 1 ties out; order 2 is paid `credit` in `installments`, the statement reporting `interest`."""
    extra = [c for c, v in (("installments", installments), ("interest_amount", interest)) if v is not None]
    header = ",".join(["Ref", "Memo", "Gross", "Fee", "Net", "ValueDate", *extra])
    cells = [v for v in (installments, interest) if v is not None]
    zeros = ["1" if c == "installments" else "0" for c in extra]
    return (header + "\n"
            + ",".join(["P1", "PAYMENT ORD2024-0000001", "100.00", "0", "100.00", "2024-01-02", *zeros]) + "\n"
            + ",".join(["P2", "PAYMENT ORD2024-0000002", credit, "0", credit, "2024-01-03", *cells]) + "\n")


def outcome(tmp_path, **kwargs):
    return run(tmp_path, write_sources(tmp_path, oms=OMS, bank=bank(**kwargs), ship=SHIP))


def unit(result):
    return result.results.set_index("order_id").loc["ORD2024-0000002"]


def test_interest_reported_on_the_statement_auto_accepts_with_a_note(tmp_path):
    result = outcome(tmp_path, credit="218.00")

    row = unit(result)
    assert (row["decision"], row["reason_codes"]) == ("AUTO_ACCEPT", "INSTALLMENT_INTEREST")
    assert "18.00" in row["explanation"] and "3 installments" in row["explanation"]
    assert "revisit" in row["explanation"].lower()


def test_annotated_auto_accepts_form_the_close_watchlist(tmp_path):
    result = outcome(tmp_path, credit="218.00")

    watchlist = result.close_watchlist
    assert watchlist["order_id"].tolist() == ["ORD2024-0000002"]
    assert watchlist["note_amount"].tolist() == [18.0]
    assert result.manifest["metrics"]["close_watchlist"] == {"units": 1, "amount": 18.0}
    exported = pd.read_csv(io.StringIO(result.bundle()["close_watchlist.csv"]))
    assert exported["order_id"].tolist() == ["ORD2024-0000002"]


def test_delta_beyond_the_stated_interest_is_an_amount_mismatch(tmp_path):
    row = unit(outcome(tmp_path, credit="228.00"))  # 10.00 more than the 18.00 interest explains

    assert (row["decision"], row["reason_codes"]) == ("EXCEPTION", "AMOUNT_MISMATCH")


def test_overpayment_with_no_interest_on_the_statement_is_an_amount_mismatch(tmp_path):
    row = unit(outcome(tmp_path, credit="210.00", interest="0"))

    assert row["reason_codes"] == "AMOUNT_MISMATCH"


def test_without_an_interest_column_a_plausible_interest_goes_to_review(tmp_path):
    row = unit(outcome(tmp_path, credit="218.00", interest=None))

    assert (row["decision"], row["reason_codes"]) == ("REVIEW", "POSSIBLE_INSTALLMENT_INTEREST")
    assert "does not report interest" in row["explanation"]


def test_single_installment_or_underpayment_is_never_interest(tmp_path):
    (tmp_path / "single").mkdir()
    (tmp_path / "under").mkdir()
    single = unit(outcome(tmp_path / "single", credit="218.00", installments="1", interest=None))
    under = unit(outcome(tmp_path / "under", credit="182.00", interest=None))
    assert single["reason_codes"] == under["reason_codes"] == "AMOUNT_MISMATCH"
