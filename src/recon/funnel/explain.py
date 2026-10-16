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


def _remitted(row: Row) -> bool:
    return _value(row, "batch_kind") == "remittance"


def _batch(row: Row) -> str:
    """'payout <id>' or 'remittance <id>': the settlement batch that pays the order."""
    return f"{_value(row, 'batch_kind') or 'payout'} {_value(row, 'batch_id') or _value(row, 'settlement_payout_id')}"


def _missing_remittance(row: Row) -> str:
    shipment = _value(row, "tracking_code")
    if shipment is None:
        return "No shipment found for this order, so no carrier remittance pays it."
    if _value(row, "shipment_returned"):
        return (f"Shipment {shipment} was returned to the seller: the carrier collected no COD, "
                "and the return is not recorded in the OMS.")
    if _value(row, "batch_id") is None:
        return f"The carrier has not put shipment {shipment} on a remittance yet."
    return f"Remittance {_value(row, 'batch_id')} pays shipment {shipment}, but no bank credit names it."


def _missing_payment(row: Row) -> str:
    due = _value(row, "due_date")
    return _missing_money(row) + (f" It was due by {due}." if due else "")


def _not_yet_due(row: Row) -> str:
    due, cutoff = _value(row, "due_date"), _value(row, "cutoff")
    if _value(row, "waiting_for") == "shipment":
        return f"Not shipped yet, and not due to ship until {due} (cut-off {cutoff}): an open item, carried " \
               "into the next period."
    when = f"not due until {due}" if due else "not due before its shipment is delivered"
    return f"Its money is {when} (cut-off {cutoff}): an open item, carried into the next period."


def _missing_money(row: Row) -> str:
    if _remitted(row):
        return _missing_remittance(row)
    if not _value(row, "settlement_feed"):
        return "No bank credit found for this order."
    payout = _value(row, "settlement_payout_id")
    if payout is None:
        return f"No line for this order in {_report(row)}."
    return f"Payout {payout} in {_report(row)} pays this order, but no bank credit names it."


def _batch_mismatch(row: Row) -> str:
    return (_difference(f"The bank credit for {_batch(row)}", _value(row, "batch_tie_out_delta"),
                        "the net of its lines").rstrip(".")
            + "; every order in the batch is reviewed.")


def _credits(row: Row) -> str:
    """'The bank credit', or 'The total of the 2 bank credits (A, B)' for a split payment."""
    refs = str(_value(row, "txn_ref") or "").split("|")
    return "The bank credit" if len(refs) < 2 else f"The total of the {len(refs)} bank credits ({', '.join(refs)})"


def _duplicate_payment(row: Row) -> str:
    return (_difference(_credits(row), _value(row, "credit_delta"), "expected").rstrip(".")
            + ", and one of them alone pays the order: a duplicate payment.")


def _tie_out(row: Row) -> str:
    refs = str(_value(row, "txn_ref") or "").split("|")
    if len(refs) < 2:
        return "Order, bank credit and shipment tie out."
    return f"Order, bank credits ({', '.join(refs)}: paid in {len(refs)} parts) and shipment tie out."


def _amount_mismatch(row: Row) -> str:
    if _remitted(row):
        return _difference("The COD collected", _value(row, "cod_amount_delta"), "the order amount")
    if _value(row, "settlement_feed"):
        return _line_amount(row)
    sentence = _difference(_credits(row), _value(row, "credit_delta"), "expected")
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
    "THREE_WAY_TIE_OUT": _tie_out,
    "TIE_OUT_PER_STATUS_POLICY": lambda row: (
        f"The order ties out on the evidence its status {_status(row)} requires."),
    "MISSING_SOURCE_EVIDENCE": lambda row: (
        "The order has no amount, so there is nothing to compare the money against."),
    "ORDER_STATUS_REQUIRES_POLICY": lambda row: (
        f"Order status {_status(row)} has no evidence policy yet; a business decision is needed."),
    "MISSING_PAYMENT": _missing_payment,
    "NOT_YET_DUE": _not_yet_due,
    "SETTLEMENT_TIE_OUT": lambda row: (
        f"The order ties out against its line in {_report(row)}, and payout "
        f"{_value(row, 'settlement_payout_id')} ties out against its bank credit."),
    "REMITTANCE_TIE_OUT": lambda row: (
        f"Shipment {_value(row, 'tracking_code')} collected the order amount, and remittance "
        f"{_value(row, 'batch_id')} ties out against its bank credit."),
    "SETTLEMENT_BATCH_MISMATCH": _batch_mismatch,
    "PAYOUT_CREDIT_AMBIGUOUS": lambda row: (
        f"Several bank credits name {_batch(row)}; a reviewer picks the one that pays it."),
    "CURRENCY_MISMATCH": _currency_mismatch,
    "INSTALLMENT_INTEREST": _installment_interest,
    "POSSIBLE_INSTALLMENT_INTEREST": _possible_interest,
    "FX_DIFFERENCE": _fx_difference,
    "AMOUNT_MISMATCH": _amount_mismatch,
    "DUPLICATE_PAYMENT": _duplicate_payment,
    "MISSING_SHIPMENT": lambda row: "No shipment found for this order." + (
        f" It was due to ship by {_value(row, 'due_date')}." if _value(row, "due_date") else ""),
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
           "settlement_gross_delta", "settlement_fee_delta", "settlement_fee_bps", "batch_tie_out_delta",
           "batch_kind", "batch_id", "tracking_code", "shipment_returned", "txn_ref", "due_date", "cutoff", "waiting_for")


def explain_all(frame: pd.DataFrame) -> list[str]:
    return [explain(row) for row in frame.reindex(columns=list(COLUMNS)).to_dict("records")]


def explain(row: Row) -> str:
    """'<sentence per reason code> Confidence <c>.' for one decided unit or orphan."""
    codes = [c for c in str(row.get("reason_codes") or "").split("|") if c]
    conversion = _conversion(row)
    return " ".join([_sentence(code, row) for code in codes] + ([conversion] if conversion else [])
                    + [f"Confidence {row['confidence']:.4g}."])
