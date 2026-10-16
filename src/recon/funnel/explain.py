"""Plain-language explanation of a decision: one sentence per reason code, then the confidence."""

from __future__ import annotations

from typing import Any, Callable

import pandas as pd

Row = dict[str, Any]


def _value(row: Row, column: str) -> Any:
    value = row.get(column)
    return None if value is None or (not isinstance(value, str) and pd.isna(value)) else value


def _difference(subject: str, delta: Any, reference: str) -> str:
    if delta is None or abs(delta) < 0.005:
        return f"{subject} differs from {reference}."
    return f"{subject} is {abs(delta):,.2f} {'more' if delta > 0 else 'less'} than {reference}."


def _fee_rate(row: Row) -> str:
    bps = _value(row, "fee_bps")
    return "the contract MDR" if bps is None else f"the contract fee ({bps / 100:.2f}%)"


def _report(row: Row) -> str:
    return f"the {_value(row, 'settlement_feed')} settlement report"


def _line_amount(row: Row) -> str:
    refund = _value(row, "settlement_refund") or 0
    if refund:
        return (f"The line in {_report(row)} refunds {refund:,.2f} to the buyer, but the order is still at its "
                "full amount.")
    return _difference("The settlement line's gross", _value(row, "settlement_gross_delta"),
                       "the order's item total")


def _line_fees(row: Row) -> str:
    bps = _value(row, "settlement_fee_bps")
    terms = "the channel's terms" if bps is None else f"the channel's terms ({bps / 100:.2f}% of gross)"
    return _difference("The fee total on the settlement line", _value(row, "settlement_fee_delta"), terms)


def _missing_payment(row: Row) -> str:
    if not _value(row, "settlement_feed"):
        return "No bank credit found for this order."
    payout = _value(row, "settlement_payout_id")
    if payout is None:
        return f"No line for this order in {_report(row)}."
    return f"Payout {payout} in {_report(row)} pays this order, but no bank credit names it."


def _batch_mismatch(row: Row) -> str:
    return (_difference(f"The bank credit for payout {_value(row, 'settlement_payout_id')}",
                        _value(row, "batch_tie_out_delta"), "the net of its lines").rstrip(".")
            + "; every order in the batch is reviewed.")


def _amount_mismatch(row: Row) -> str:
    if _value(row, "settlement_feed"):
        return _line_amount(row)
    sentence = _difference("The bank credit", _value(row, "credit_delta"), "expected")
    cod_delta = _value(row, "cod_amount_delta")
    if row.get("payment_method") == "COD" and cod_delta:
        sentence += " " + _difference("The COD collected", cod_delta, "the order amount")
    return sentence


def _currency_mismatch(row: Row) -> str:
    credit, order = _value(row, "currency_bank"), _value(row, "currency_oms")
    if _value(row, "fx_rate_missing"):
        return (f"No FX rate from {credit} to {order} on or before the value date, "
                "so the amounts were not compared.")
    if credit and order:
        return (f"The bank credit is in {credit} but the order is in {order}; "
                "amounts in different currencies are not compared.")
    return "The bank credit and the order are in different currencies; amounts are not compared."


def _fx_difference(row: Row) -> str:
    delta, expected = _value(row, "credit_delta"), _value(row, "expected_credit")
    share = f" ({abs(delta) / abs(expected):.1%})" if delta is not None and expected else ""
    return (_difference("The converted bank credit", delta, "expected").rstrip(".")
            + f"{share}, within the FX tolerance.")


def _conversion(row: Row) -> str | None:
    rate = _value(row, "fx_rate")
    if rate is None or not _value(row, "fx_converted"):
        return None
    return (f"Credit converted from {_value(row, 'currency_bank')} to {_value(row, 'currency_oms')} "
            f"at {rate:.6g} ({_value(row, 'fx_rate_source')}, {_value(row, 'fx_rate_date')}).")


def _installments(row: Row) -> str:
    count = _value(row, "installments") or _value(row, "installments_bank")
    return f"{int(count)} installments" if count else "installments"


def _installment_interest(row: Row) -> str:
    delta = _value(row, "credit_delta") or 0.0
    return (f"The bank credit is {delta:,.2f} more than the order amount, matching the installment interest the "
            f"statement reports on {_installments(row)}; auto-accepted with a note. "
            "Revisit first if the period does not close.")


def _possible_interest(row: Row) -> str:
    delta, expected = _value(row, "credit_delta") or 0.0, _value(row, "expected_credit")
    share = f" ({delta / abs(expected):.1%})" if expected else ""
    return (f"The bank credit is {delta:,.2f}{share} more than the order amount on a payment in "
            f"{_installments(row)}. This may be installment interest, but the statement does not report "
            "interest, so a reviewer confirms it.")


def _status(row: Row) -> str:
    status = _value(row, "order_status")
    return f"'{status}'" if status else "(blank)"


def _candidates(row: Row) -> str:
    listed = _value(row, "link_candidates")
    count = len(listed.split("|")) if listed else 0
    return (f"No bank credit names this order; {count} unreferenced credit(s) with a matching amount "
            "are listed as candidates.")


SENTENCES: dict[str, Callable[[Row], str]] = {
    "THREE_WAY_TIE_OUT": lambda row: "Order, bank credit and shipment tie out.",
    "TIE_OUT_PER_STATUS_POLICY": lambda row: (
        f"The order ties out on the evidence its status {_status(row)} requires."),
    "MISSING_SOURCE_EVIDENCE": lambda row: (
        "The order has no amount, so there is nothing to compare the money against."),
    "ORDER_STATUS_REQUIRES_POLICY": lambda row: (
        f"Order status {_status(row)} has no evidence policy yet; a business decision is needed."),
    "MISSING_PAYMENT": _missing_payment,
    "SETTLEMENT_TIE_OUT": lambda row: (
        f"The order ties out against its line in {_report(row)}, and payout "
        f"{_value(row, 'settlement_payout_id')} ties out against its bank credit."),
    "SETTLEMENT_BATCH_MISMATCH": _batch_mismatch,
    "PAYOUT_CREDIT_AMBIGUOUS": lambda row: (
        f"Several bank credits name payout {_value(row, 'settlement_payout_id')}; a reviewer picks the one "
        "that pays it."),
    "CURRENCY_MISMATCH": _currency_mismatch,
    "INSTALLMENT_INTEREST": _installment_interest,
    "POSSIBLE_INSTALLMENT_INTEREST": _possible_interest,
    "FX_DIFFERENCE": _fx_difference,
    "AMOUNT_MISMATCH": _amount_mismatch,
    "MISSING_SHIPMENT": lambda row: "No shipment found for this order.",
    "FEE_MISMATCH": lambda row: (_line_fees(row) if _value(row, "settlement_feed")
                                 else _difference("The bank fee", _value(row, "fee_delta"), _fee_rate(row))),
    "COD_FEE_MISMATCH": lambda row: _difference("The COD fee", _value(row, "cod_fee_delta"), "the contract fee"),
    "TIMING_MISMATCH": lambda row: "The shipment was picked up after it was delivered.",
    "LINK_CANDIDATES": _candidates,
    "ORPHAN_PAYMENT": lambda row: "This bank credit matches no order.",
    "ORPHAN_SHIPMENT": lambda row: "This shipment matches no order.",
}


def _sentence(code: str, row: Row) -> str:
    if code in SENTENCES:
        return SENTENCES[code](row)
    if code.startswith("DUPLICATE_") and code.endswith("_KEY"):
        feed = code.removeprefix("DUPLICATE_").removesuffix("_KEY").lower()
        return f"A {feed} key of this order occurs more than once; a reviewer picks the right record."
    return f"{code}."


# Columns the sentences read; everything else is irrelevant to the explanation.
COLUMNS = ("reason_codes", "confidence", "payment_method", "order_status", "credit_delta", "cod_amount_delta",
           "fee_delta", "fee_bps", "cod_fee_delta", "link_candidates", "currency_oms", "currency_bank",
           "expected_credit", "fx_rate", "fx_rate_source", "fx_rate_date", "fx_converted", "fx_rate_missing",
           "installments", "installments_bank", "settlement_feed", "settlement_payout_id", "settlement_refund",
           "settlement_gross_delta", "settlement_fee_delta", "settlement_fee_bps", "batch_tie_out_delta")


def explain_all(frame: pd.DataFrame) -> list[str]:
    return [explain(row) for row in frame.reindex(columns=list(COLUMNS)).to_dict("records")]


def explain(row: Row) -> str:
    """'<sentence per reason code> Confidence <c>.' for one decided unit or orphan."""
    codes = [c for c in str(row.get("reason_codes") or "").split("|") if c]
    conversion = _conversion(row)
    return " ".join([_sentence(code, row) for code in codes] + ([conversion] if conversion else [])
                    + [f"Confidence {row['confidence']:.4g}."])
