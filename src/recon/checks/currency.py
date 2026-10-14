"""Warning check: bank credits whose currency differs from the order they name.

A feed may carry several currencies; each linked pair is compared in its own currency.
Only a credit whose currency disagrees with its order's is an issue.
"""

from __future__ import annotations

import pandas as pd

from recon.checks import register, warning
from recon.checks.integrity import exclude_option
from recon.config import RunConfig
from recon.funnel.linking import bank_order_refs
from recon.health import CONVERT_CURRENCY, CORRECT_CURRENCY, ROUTE_TO_REVIEW, FixOption, Issue
from recon.schema import CURRENCY, ORDER_REF_COLUMNS, SOURCE_ROW, review_column

REASON = "CURRENCY_MISMATCH"


def currency_codes(values: pd.Series) -> pd.Series:
    return values.astype("string").str.strip().str.upper()


def _rows(frame: pd.DataFrame) -> list[int]:
    return [int(r) for r in frame[SOURCE_ROW]]


@register
def currency_mismatch(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    oms, bank = feeds.get("oms"), feeds.get("bank")
    order_id = ORDER_REF_COLUMNS["oms"]
    if config.resolved_policy.fx is not None:
        return []  # converted in the funnel; credits without a rate become CURRENCY_MISMATCH there
    if oms is None or bank is None or CURRENCY not in oms or CURRENCY not in bank or order_id not in oms:
        return []
    if not ({ORDER_REF_COLUMNS["bank"], "description"} & set(bank.columns)):
        return []
    order_currency = (oms.dropna(subset=[order_id]).drop_duplicates(order_id, keep=False)
                      .set_index(order_id)[CURRENCY].pipe(currency_codes))
    expected = bank_order_refs(bank).map(order_currency)
    actual = currency_codes(bank[CURRENCY])
    routed = (bank[review_column("bank")].notna() if review_column("bank") in bank
              else pd.Series(False, index=bank.index))  # already routed to review: handled
    mask = (expected.notna() & actual.notna() & expected.ne(actual) & ~routed).fillna(False).astype(bool)
    if not mask.any():
        return []
    mismatched = bank[mask]
    corrections = tuple(
        FixOption(CORRECT_CURRENCY, f"Correct the currency code to {code} (the order's currency)",
                  {"feed": "bank", "source_rows": _rows(mismatched[expected[mask].eq(code)]), "currency": code})
        for code in sorted(expected[mask].unique())
    )
    fixes = (FixOption(CONVERT_CURRENCY, "Convert into the order currency using a rate source",
                       {"rate_column": None, "rate_table": None, "tolerance": 0.01}),) + corrections + (
        FixOption(ROUTE_TO_REVIEW, "Route these orders to review",
                  {"feed": "bank", "source_rows": _rows(mismatched), "reason": REASON}),
        exclude_option("bank", mismatched, "Exclude these credits (they leave the reconciliation)"),
    )
    pairs = ", ".join(f"{a} credit for a {e} order" for a, e in sorted(set(zip(actual[mask], expected[mask]))))
    return [warning("currency_mismatch:bank", "currency_mismatch", "bank", bank, mask,
                    f"bank: {int(mask.sum())} credit(s) in another currency than their order ({pairs}); "
                    "amounts in different currencies are not compared", fixes)]
