"""Rule checks on linked units, one funnel step per reason code, in the documented order.

The first rule check measures deltas and tolerances; later checks reuse those columns.
A unit is decided by the first check it fails.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd

from recon.contracts import fee
from recon.funnel.base import FunnelContext, FunnelStep, StepOutcome, cod_remittance, decide, split
from recon.funnel.fx import lookup_rates
from recon.funnel.reasons import exception
from recon.schema import CURRENCY, INSTALLMENTS, INTEREST_AMOUNT, REVIEW


def _within(delta: pd.Series, tolerance: float) -> pd.Series:
    """Absolute tolerance, inclusive; the epsilon absorbs float noise such as 20.01 - 20.00."""
    return delta.abs() <= tolerance + 1e-9


def _measure(units: pd.DataFrame, ctx: FunnelContext) -> pd.DataFrame:
    if "credit_delta" in units:
        return units
    policy, out = ctx.policy, units.copy()
    for column in ("requires_payment", "requires_shipment"):
        if column not in out:
            out[column] = True
    _currencies(out, ctx)
    out["bank_found"] = out["txn_ref"].notna()
    out["shipment_found"] = out["tracking_code"].notna()
    out["tracking_exact"] = out["tracking_no"].fillna("").eq(out["tracking_code"].fillna(""))
    out["amount_delta"] = out["credit_amount"].fillna(0) - out["order_amount"].fillna(0)
    out["cod_amount_delta"] = out["cod_collected"].fillna(0) - out["order_amount"].fillna(0)
    out["expected_credit"] = out["order_amount"].fillna(0).where(out["payment_method"].eq("PREPAID"),
                                                                 cod_remittance(out))
    out["credit_delta"] = out["credit_amount"].fillna(0) - out["expected_credit"]
    out["expected_mdr"] = out["credit_amount"].fillna(0).where(
        out["payment_method"].eq("PREPAID"), 0
    ).map(lambda x: fee(int(x), policy.mdr_bps))
    out["expected_cod_fee"] = out["cod_collected"].fillna(0).map(lambda x: fee(int(x), policy.cod_fee_bps))
    out["fee_delta"] = out["fee_amount"].fillna(0) - out["expected_mdr"]
    out["cod_fee_delta"] = out["cod_fee"].fillna(0) - out["expected_cod_fee"]
    out["amount_within_tolerance"] = (_within(out["credit_delta"], policy.amount_tolerance)) & (
        ~out["payment_method"].eq("COD") | (_within(out["cod_amount_delta"], policy.amount_tolerance))
    )
    out["fee_within_tolerance"] = _within(out["fee_delta"], policy.amount_tolerance)
    out["cod_fee_within_tolerance"] = _within(out["cod_fee_delta"], policy.amount_tolerance)
    _installment_interest(out, policy)
    out["fx_within_tolerance"] = out["fx_converted"] & (
        out["credit_delta"].abs() <= policy.fx.tolerance * out["expected_credit"].abs() + 1e-9
        if policy.fx else False)
    out["timing_ok"] = True
    if "picked_at" in out and "delivered_at" in out:
        picked = ctx.dates("logistics", out["picked_at"])
        delivered = ctx.dates("logistics", out["delivered_at"])
        out["timing_ok"] = ~(picked > delivered)
    # A unit reaching the decision passed every check; a linked shipment must also agree on tracking number.
    out["tracking_consistent"] = ~out["shipment_found"] | out["tracking_exact"]
    return out


def _bank_number(out: pd.DataFrame, column: str) -> pd.Series | None:
    """A numeric bank column (suffixed on the join when the name clashed), or None when the feed lacks it."""
    name = next((c for c in (column, f"{column}_bank") if c in out), None)
    return pd.to_numeric(out[name], errors="coerce") if name else None


def _installment_interest(out: pd.DataFrame, policy) -> None:
    """Explain a credit above the expected amount as installment interest.

    With an interest column, a delta equal to the stated interest is explained (auto-accepted with a
    note). Without one, a positive delta within `installment_interest_max` on an installment payment
    is only possible interest: a reviewer confirms it. Nothing else counts as interest.
    """
    installments, interest = _bank_number(out, INSTALLMENTS), _bank_number(out, INTEREST_AMOUNT)
    over = out["bank_found"] & ~out["amount_within_tolerance"] & (out["credit_delta"] > policy.amount_tolerance)
    paid_in_parts = installments.fillna(0) > 1 if installments is not None else pd.Series(False, index=out.index)
    if interest is not None:
        stated = interest.fillna(0)
        out["interest_explained"] = over & (stated > 0) & _within(out["credit_delta"] - stated, policy.amount_tolerance)
        out["interest_possible"] = False
    else:
        out["interest_explained"] = False
        out["interest_possible"] = over & paid_in_parts & (
            out["credit_delta"] <= policy.installment_interest_max * out["expected_credit"].abs())


def _currencies(out: pd.DataFrame, ctx: FunnelContext) -> None:
    """Compare like for like; with FX configured, convert mismatched credits into the order currency.

    Linked order and credit both carry a currency when the columns were suffixed on the join.
    Converted units keep the original amounts and record the rate, its source and its date.
    """
    oms_ccy, bank_ccy = f"{CURRENCY}_oms", f"{CURRENCY}_bank"
    fx = ctx.policy.fx
    # Read the bank's own rate column before the fx_* outputs are initialised (it may share their name).
    candidates = (fx.rate_column, f"{fx.rate_column}_bank") if fx and fx.rate_column else ()
    rate_column = next((c for c in candidates if c in out), None)
    column_rates = out[rate_column].copy() if rate_column else None
    out["currency_ok"], out["fx_converted"], out["fx_rate_missing"] = True, False, False
    for column in ("fx_rate", "fx_rate_source", "fx_rate_date"):
        out[column] = float("nan") if column == "fx_rate" else None
    if oms_ccy not in out or bank_ccy not in out:
        return
    order, credit = (out[c].astype("string").str.strip().str.upper() for c in (oms_ccy, bank_ccy))
    mismatch = (order.notna() & credit.notna() & order.ne(credit)).fillna(False).astype(bool)
    out["currency_ok"] = ~mismatch
    if fx is None or not mismatch.any():
        return
    rates = lookup_rates(credit[mismatch], order[mismatch], ctx.dates("bank", out.loc[mismatch, "value_date"]),
                         column_rates[mismatch] if column_rates is not None else None, fx.rate_column,
                         ctx.fx_rates, fx.rate_table)
    out.loc[mismatch, list(rates.columns)] = rates
    found = mismatch & out["fx_rate"].notna()
    for column in ("credit_amount", "fee_amount", "net_amount"):
        out[f"{column}_original"] = out[column].where(found)
        out.loc[found, column] = out.loc[found, column] * out.loc[found, "fx_rate"]
    out["fx_converted"] = found
    out["fx_rate_missing"] = mismatch & ~found
    out.loc[found, "currency_ok"] = True


def _rule(reason: str, fails: Callable[[pd.DataFrame], pd.Series]):
    def check(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
        units = _measure(units, ctx)
        failed, remaining = split(units, fails(units).astype(bool))
        return StepOutcome(decided=exception(failed, reason), remaining=remaining)

    check.__doc__ = f"Units failing this check become EXCEPTION({reason})."
    return check


POSSIBLE_INTEREST_CONFIDENCE = 0.5


def possible_installment_interest(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
    """Units whose only discrepancy may be unstated installment interest go to REVIEW (low priority)."""
    units = _measure(units, ctx)
    possible, remaining = split(units, units["interest_possible"].astype(bool))
    decided = decide(possible, REVIEW, POSSIBLE_INTEREST_CONFIDENCE, "POSSIBLE_INSTALLMENT_INTEREST").assign(priority=1)
    return StepOutcome(decided=decided, remaining=remaining)


RULE_STEPS: list[FunnelStep] = [
    FunnelStep("missing_payment", _rule("MISSING_PAYMENT", lambda u: u["requires_payment"] & ~u["bank_found"])),
    FunnelStep("currency_mismatch", _rule("CURRENCY_MISMATCH", lambda u: u["bank_found"] & ~u["currency_ok"])),
    FunnelStep("fx_difference", _rule("FX_DIFFERENCE", lambda u: u["bank_found"] & ~u["amount_within_tolerance"]
                                      & u["fx_within_tolerance"])),
    FunnelStep("amount_mismatch", _rule("AMOUNT_MISMATCH", lambda u: u["bank_found"] & ~u["amount_within_tolerance"]
                                        & ~u["interest_explained"] & ~u["interest_possible"])),
    FunnelStep("missing_shipment", _rule("MISSING_SHIPMENT",
                                         lambda u: u["requires_shipment"] & ~u["shipment_found"])),
    FunnelStep("fee_mismatch", _rule("FEE_MISMATCH", lambda u: u["bank_found"] & ~u["fee_within_tolerance"])),
    FunnelStep("cod_fee_mismatch", _rule("COD_FEE_MISMATCH",
                                         lambda u: u["shipment_found"] & ~u["cod_fee_within_tolerance"])),
    FunnelStep("timing_mismatch", _rule("TIMING_MISMATCH", lambda u: u["shipment_found"] & ~u["timing_ok"])),
    FunnelStep("possible_installment_interest", possible_installment_interest),
]
