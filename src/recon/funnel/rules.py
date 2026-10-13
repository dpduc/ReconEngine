"""Rule checks on linked units, one funnel step per reason code, in the documented order.

The first rule check measures deltas and tolerances; later checks reuse those columns.
A unit is decided by the first check it fails.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd

from recon.contracts import fee
from recon.funnel.base import FunnelContext, FunnelStep, StepOutcome, split
from recon.funnel.reasons import exception


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
    out["bank_found"] = out["txn_ref"].notna()
    out["shipment_found"] = out["tracking_code"].notna()
    out["tracking_exact"] = out["tracking_no"].fillna("").eq(out["tracking_code"].fillna(""))
    out["amount_delta"] = out["credit_amount"].fillna(0) - out["order_amount"].fillna(0)
    out["cod_amount_delta"] = out["cod_collected"].fillna(0) - out["order_amount"].fillna(0)
    out["expected_credit"] = out["order_amount"].fillna(0).where(
        out["payment_method"].eq("PREPAID"),
        out["cod_collected"].fillna(0) - out["ship_fee"].fillna(0) - out["cod_fee"].fillna(0),
    )
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
    out["timing_ok"] = True
    if "picked_at" in out and "delivered_at" in out:
        picked = pd.to_datetime(out["picked_at"], errors="coerce", format="ISO8601")
        delivered = pd.to_datetime(out["delivered_at"], errors="coerce", format="ISO8601")
        out["timing_ok"] = ~(picked > delivered)
    out["candidate_key_exact"] = out["bank_found"] & out["shipment_found"] & out["tracking_exact"]
    # Every check below passed; a linked shipment must also agree on tracking number.
    out["clean"] = ~out["shipment_found"] | out["tracking_exact"]
    return out


def _rule(reason: str, fails: Callable[[pd.DataFrame], pd.Series]):
    def check(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
        units = _measure(units, ctx)
        failed, remaining = split(units, fails(units).astype(bool))
        return StepOutcome(decided=exception(failed, reason), remaining=remaining)

    check.__doc__ = f"Units failing this check become EXCEPTION({reason})."
    return check


RULE_STEPS: list[FunnelStep] = [
    FunnelStep("missing_payment", _rule("MISSING_PAYMENT", lambda u: u["requires_payment"] & ~u["bank_found"])),
    FunnelStep("amount_mismatch", _rule("AMOUNT_MISMATCH", lambda u: u["bank_found"] & ~u["amount_within_tolerance"])),
    FunnelStep("missing_shipment", _rule("MISSING_SHIPMENT",
                                         lambda u: u["requires_shipment"] & ~u["shipment_found"])),
    FunnelStep("fee_mismatch", _rule("FEE_MISMATCH", lambda u: u["bank_found"] & ~u["fee_within_tolerance"])),
    FunnelStep("cod_fee_mismatch", _rule("COD_FEE_MISMATCH",
                                         lambda u: u["shipment_found"] & ~u["cod_fee_within_tolerance"])),
    FunnelStep("timing_mismatch", _rule("TIMING_MISMATCH", lambda u: u["shipment_found"] & ~u["timing_ok"])),
]
