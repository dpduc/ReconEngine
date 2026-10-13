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


def _amount_mismatch(row: Row) -> str:
    sentence = _difference("The bank credit", _value(row, "credit_delta"), "expected")
    cod_delta = _value(row, "cod_amount_delta")
    if row.get("payment_method") == "COD" and cod_delta:
        sentence += " " + _difference("The COD collected", cod_delta, "the order amount")
    return sentence


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
    "MISSING_PAYMENT": lambda row: "No bank credit found for this order.",
    "AMOUNT_MISMATCH": _amount_mismatch,
    "MISSING_SHIPMENT": lambda row: "No shipment found for this order.",
    "FEE_MISMATCH": lambda row: _difference("The bank fee", _value(row, "fee_delta"), "the contract MDR"),
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
           "fee_delta", "cod_fee_delta", "link_candidates")


def explain_all(frame: pd.DataFrame) -> list[str]:
    return [explain(row) for row in frame.reindex(columns=list(COLUMNS)).to_dict("records")]


def explain(row: Row) -> str:
    """'<sentence per reason code> Confidence <c>.' for one decided unit or orphan."""
    codes = [c for c in str(row.get("reason_codes") or "").split("|") if c]
    return " ".join([_sentence(code, row) for code in codes] + [f"Confidence {row['confidence']:.4g}."])
