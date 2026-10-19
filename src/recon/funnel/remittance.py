"""COD money chain (ADR 0001): order ──order ref──▶ shipment ──remittance id──▶ bank credit.

A carrier pays the COD it collected in one bank credit per **remittance**, less shipping and COD
fees, for every parcel on it (prepaid parcels too: they only cost their shipping). A remittance is
a settlement batch like a payout: tied out on its own (the credit equals its shipments' COD less
fees), while each COD order is checked against its own shipment:

    no shipment for the order               EXCEPTION(MISSING_SHIPMENT)
    shipment returned to the seller         EXCEPTION(MISSING_PAYMENT)
    shipment on no remittance yet           EXCEPTION(MISSING_PAYMENT)
    COD collected differs from the order    EXCEPTION(AMOUNT_MISMATCH)
    COD fee off the carrier's terms         EXCEPTION(COD_FEE_MISMATCH)
    remittance has no bank credit           EXCEPTION(MISSING_PAYMENT)
    remittance does not tie out             REVIEW(SETTLEMENT_BATCH_MISMATCH)
    several credits name the remittance     REVIEW(PAYOUT_CREDIT_AMBIGUOUS)
    otherwise                               AUTO_ACCEPT(REMITTANCE_TIE_OUT)

The payment checks apply when the order's status requires payment. A returned shipment collects
nothing and pays no COD fee, so the COD fee check covers the return terms; return shipping is
not checked (the config has no shipping terms).
"""

from __future__ import annotations

import pandas as pd

from recon.config import Policy
from recon.contracts import currency_fee
from recon.funnel.base import FunnelContext, StepOutcome, cod_remittance, decide, fee_currency, pass_on, split
from recon.funnel.decision import TIE_OUT_CONFIDENCE
from recon.funnel.linking import attach_bank, attach_exact, bank_frame
from recon.funnel.periods import missing_payment, missing_shipment
from recon.funnel.reasons import exception
from recon.funnel.rules import within
from recon.funnel.settlement import (
    BATCH_MISMATCH,
    BATCH_REVIEW_CONFIDENCE,
    CREDIT_AMBIGUOUS,
    Settlements,
    batch_frame,
    payout_credits,
    tie_out,
)
from recon.schema import AUTO_ACCEPT, CHANNEL, REMITTANCE_ID, REVIEW, SHIPMENT_STATUS, SOURCE_ROW

LOGISTICS = "logistics"
REMITTANCE = "remittance"  # the batch kind, and the bank link pass of a COD order paid by one
RETURNED = "RETURNED"


def remit_batches(feeds: dict[str, pd.DataFrame], policy: Policy) -> Settlements:
    """Each remittance on the logistics report, tied out against the bank credit naming it.

    The batch's `channel` is its carrier (when the report has a `carrier` column)."""
    shipments = feeds[LOGISTICS]
    if REMITTANCE_ID not in shipments:
        return Settlements(batch_frame([]), frozenset())
    shipments = shipments[shipments[REMITTANCE_ID].notna()]
    ids = shipments[REMITTANCE_ID].astype(str)
    bank = feeds["bank"]
    credits = payout_credits(bank, ids.unique())
    by_remittance = credits.groupby("payout_id")["bank_pos"].agg(list)
    rows = []
    for remittance_id, group in shipments.groupby(ids, sort=True):
        carriers = group["carrier"].dropna() if "carrier" in group else pd.Series(dtype=object)
        rows.append(tie_out(bank, by_remittance.get(remittance_id, []), policy.amount_tolerance, kind="Remittance",
                            settlement_feed=LOGISTICS, channel=carriers.iloc[0] if len(carriers) else None,
                            payout_id=remittance_id, lines=len(group),
                            gross=float(group["cod_collected"].fillna(0).sum()),
                            fees=float((group["ship_fee"].fillna(0) + group["cod_fee"].fillna(0)).sum()),
                            refund=0.0, net=float(cod_remittance(group).sum())))
    return Settlements(batch_frame(rows), frozenset(bank.loc[credits["bank_pos"], SOURCE_ROW].astype(int)),
                       frozenset(shipments[SOURCE_ROW].astype(int)))


def _logistics(units: pd.DataFrame, column: str) -> pd.Series:
    """A logistics column on linked units (suffixed when the OMS has a column of the same name)."""
    name = next((c for c in (f"{column}_logistics", column) if c in units), None)
    return units[name] if name else pd.Series(None, index=units.index, dtype=object)


def _measure(units: pd.DataFrame, ctx: FunnelContext) -> pd.DataFrame:
    """Join each COD order's shipment to its remittance; measure the COD and its fee against the order."""
    out = units.copy()
    out["shipment_found"] = out["tracking_code"].notna()
    status = _logistics(out, SHIPMENT_STATUS).fillna("").astype(str).str.strip().str.upper()
    out["shipment_returned"] = out["shipment_found"] & status.eq(RETURNED)
    out["batch_kind"], out["batch_feed"] = REMITTANCE, LOGISTICS
    out["batch_id"] = _logistics(out, REMITTANCE_ID)
    batches = ctx.batches[ctx.batches["settlement_feed"].eq(LOGISTICS)].set_index("payout_id") \
        if ctx.batches is not None else pd.DataFrame(columns=["reason_codes", "tie_out_delta", "bank_pos"])
    out["batch_reason"] = out["batch_id"].map(batches["reason_codes"])
    out["batch_tie_out_delta"] = out["batch_id"].map(batches["tie_out_delta"])
    out["batch_bank_pos"] = out["batch_id"].map(batches["bank_pos"])
    collected = out["cod_collected"].fillna(0)
    out["cod_amount_delta"] = collected - out["order_amount"].fillna(0)
    out["expected_cod_fee"] = [currency_fee(x, ctx.policy.cod_fee_bps, c) for x, c in zip(collected, fee_currency(out), strict=True)]
    out["cod_fee_delta"] = out["cod_fee"].fillna(0) - out["expected_cod_fee"]
    return out


def settle_cod(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
    """Decide every COD order of a remittance channel along its chain (see the module docstring)."""
    if not ctx.cod_channels or CHANNEL not in units or "payment_method" not in units:
        return pass_on(units)
    chain = units[CHANNEL].fillna("").astype(str).str.strip().isin(list(ctx.cod_channels)) \
        & units["payment_method"].eq("COD")
    cod, remaining = split(units, chain)
    if cod.empty:
        return pass_on(units)
    measured = _measure(attach_exact(cod, ctx), ctx)
    credited = measured["batch_bank_pos"].notna()
    links = pd.DataFrame({"unit_id": measured.loc[credited, "unit_id"],
                          "bank_pos": measured.loc[credited, "batch_bank_pos"].astype(int)})
    measured = attach_bank(measured, bank_frame(ctx), links, REMITTANCE)

    tolerance, decided, open_ = ctx.policy.amount_tolerance, [], measured
    shipped, pay = open_["shipment_found"], open_["requires_payment"]
    waiting = "MISSING_PAYMENT, not yet"  # money that has not arrived: OPEN while not yet due
    checks = [
        ("MISSING_SHIPMENT", ~shipped & open_["requires_shipment"]),
        (waiting, ~shipped & pay),
        ("MISSING_PAYMENT", open_["shipment_returned"] & pay),
        (waiting, open_["batch_id"].isna() & pay),
        ("AMOUNT_MISMATCH", pay & ~within(open_["cod_amount_delta"], tolerance)),
        ("COD_FEE_MISMATCH", shipped & ~within(open_["cod_fee_delta"], tolerance)),
        (waiting, pay & open_["batch_reason"].eq("MISSING_PAYMENT")),
    ]
    for reason, mask in checks:
        failed, open_ = split(open_, mask.reindex(open_.index).fillna(False).astype(bool))
        hold = {waiting: missing_payment, "MISSING_SHIPMENT": missing_shipment}.get(reason)
        decided.append(hold(failed, ctx) if hold else exception(failed, reason))
    review, open_ = split(open_, open_["batch_reason"].isin([BATCH_MISMATCH, CREDIT_AMBIGUOUS]))
    decided.append(decide(review, REVIEW, BATCH_REVIEW_CONFIDENCE, review["batch_reason"]))
    reasons = open_["requires_payment"].map({True: "REMITTANCE_TIE_OUT", False: "TIE_OUT_PER_STATUS_POLICY"})
    decided.append(decide(open_, AUTO_ACCEPT, TIE_OUT_CONFIDENCE, reasons))
    return StepOutcome(decided=pd.concat(decided).drop(columns=["batch_bank_pos"]), remaining=remaining)


def remittance_links(results: pd.DataFrame, batches: pd.DataFrame) -> pd.DataFrame:
    """`results` with `remittance_txn_ref`: the bank credit of the remittance each unit's shipment is on
    (a prepaid parcel's shipping is netted there too)."""
    remitted = batches[batches["settlement_feed"].eq(LOGISTICS)].set_index("payout_id")["txn_ref"]
    if remitted.empty or REMITTANCE_ID not in results and f"{REMITTANCE_ID}_logistics" not in results:
        return results
    return results.assign(remittance_txn_ref=_logistics(results, REMITTANCE_ID).map(remitted))
