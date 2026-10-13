"""Rule checks on linked units: deltas, tolerances and reason codes."""

from __future__ import annotations

import pandas as pd

from recon.contracts import fee
from recon.funnel.base import FunnelContext, StepOutcome, decide, split

EXCEPTION_CONFIDENCE = 0.15


def _features(units: pd.DataFrame, ctx: FunnelContext) -> pd.DataFrame:
    policy, out = ctx.policy, units.copy()
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
    out["amount_within_tolerance"] = (out["credit_delta"].abs() <= policy.amount_tolerance) & (
        ~out["payment_method"].eq("COD") | (out["cod_amount_delta"].abs() <= policy.amount_tolerance)
    )
    out["fee_within_tolerance"] = out["fee_delta"].abs() <= policy.amount_tolerance
    out["cod_fee_within_tolerance"] = out["cod_fee_delta"].abs() <= policy.amount_tolerance
    out["candidate_key_exact"] = out["bank_found"] & out["shipment_found"] & out["tracking_exact"]
    out["clean"] = (
        out["candidate_key_exact"] & out["amount_within_tolerance"]
        & out["fee_within_tolerance"] & out["cod_fee_within_tolerance"]
    )
    out["reason_codes"] = [_reason_codes(row) for row in out.itertuples(index=False)]
    return out


def _reason_codes(row) -> str:
    reasons: list[str] = []
    if not row.bank_found:
        reasons.append("MISSING_PAYMENT")
    if not row.shipment_found:
        reasons.append("MISSING_SHIPMENT")
    if row.bank_found and not row.amount_within_tolerance:
        reasons.append("AMOUNT_MISMATCH")
    if row.bank_found and not row.fee_within_tolerance:
        reasons.append("FEE_MISMATCH")
    if row.shipment_found and not row.cod_fee_within_tolerance:
        reasons.append("COD_FEE_MISMATCH")
    return "|".join(reasons) or "THREE_WAY_TIE_OUT"


def rule_checks(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
    """Units with a missing leg or a mismatch become exceptions; the rest pass on."""
    units = _features(units, ctx)
    failed, remaining = split(units, units["reason_codes"].str.contains("MISSING|MISMATCH"))
    return StepOutcome(decided=decide(failed, "EXCEPTION", EXCEPTION_CONFIDENCE), remaining=remaining)
