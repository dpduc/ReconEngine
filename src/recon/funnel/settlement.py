"""Marketplace money chain (ADR 0001): order ──channel order no──▶ settlement line ──payout id──▶ bank credit.

A **settlement batch** is one payout of one settlement report: its lines and the bank credit that
pays them. The batch is checked on its own (tie-out: the credit equals the sum of its lines' net)
and each order is checked against its own line (gross, refund, fees). The order stays the unit:

    no line for the order               EXCEPTION(MISSING_PAYMENT)
    line refunds, or gross differs      EXCEPTION(AMOUNT_MISMATCH)
    line fees off the channel's terms   EXCEPTION(FEE_MISMATCH)
    payout has no bank credit           EXCEPTION(MISSING_PAYMENT)
    batch does not tie out              REVIEW(SETTLEMENT_BATCH_MISMATCH)
    several credits name the payout     REVIEW(PAYOUT_CREDIT_AMBIGUOUS)
    otherwise                           AUTO_ACCEPT(SETTLEMENT_TIE_OUT)

A problem on an order's own line is reported on that order even when its batch is also off.
The platform ships a marketplace order, so no shipment is required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

from recon.config import Channel, Policy
from recon.funnel.base import FunnelContext, StepOutcome, decide, pass_on, split
from recon.funnel.decision import TIE_OUT_CONFIDENCE
from recon.funnel.linking import attach_bank, bank_frame, link_one_to_one, attach_exact
from recon.funnel.reasons import exception
from recon.funnel.rules import within
from recon.schema import (
    AUTO_ACCEPT,
    CHANNEL,
    CHANNEL_ORDER_NO,
    EXCEPTION,
    FEE_PREFIX,
    INGESTED_AT,
    ITEM_TOTAL,
    PAYOUT_ID,
    REVIEW,
    SOURCE_ROW,
)

PREFIX = "settlement_"  # every settlement line column on a unit, lineage included (settlement_source_row)
TOKEN = re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]*")
BATCH_REVIEW_CONFIDENCE = 0.5
TIE_OUT, BATCH_MISMATCH, CREDIT_AMBIGUOUS = "BATCH_TIE_OUT", "SETTLEMENT_BATCH_MISMATCH", "PAYOUT_CREDIT_AMBIGUOUS"
BATCH_DECISIONS = {TIE_OUT: AUTO_ACCEPT, "MISSING_PAYMENT": EXCEPTION, BATCH_MISMATCH: REVIEW,
                   CREDIT_AMBIGUOUS: REVIEW}
BATCH_COLUMNS = ("settlement_feed", "channel", "payout_id", "lines", "gross", "fees", "refund", "net",
                 "txn_ref", "bank_source_row", "credit_amount", "tie_out_delta", "decision", "reason_codes",
                 "explanation")


def payout_credits(bank: pd.DataFrame, payout_ids) -> pd.DataFrame:
    """(payout_id, bank_pos) for each bank row naming a known payout: its `payout_id` column when mapped,
    else a whole word of its description."""
    ids = {str(i) for i in payout_ids if isinstance(i, str) and i}
    if PAYOUT_ID in bank.columns:
        named = bank[PAYOUT_ID].astype("string")
        rows = [(str(pid), pos) for pos, pid in named.items() if pid in ids]
    else:
        rows = [(token, pos) for pos, text in bank["description"].astype(str).items()
                for token in dict.fromkeys(TOKEN.findall(text)) if token in ids]
    return pd.DataFrame(rows, columns=["payout_id", "bank_pos"])


def _report_channels(marketplaces: dict[str, Channel]) -> dict[str, tuple[str, Channel]]:
    """Settlement feed -> (the first channel settled by it, that channel)."""
    out: dict[str, tuple[str, Channel]] = {}
    for name, channel in marketplaces.items():
        out.setdefault(channel.settlement, (name, channel))
    return out


def _fees(frame: pd.DataFrame) -> pd.Series:
    """A line's fees: the sum of its `fee_*` columns, sign-free (reports print fees as + or -)."""
    columns = [c for c in frame.columns if str(c).startswith(FEE_PREFIX)]
    return frame[columns].abs().fillna(0).sum(axis=1) if columns else pd.Series(0.0, index=frame.index)


def _refund(frame: pd.DataFrame) -> pd.Series:
    return frame["refund"].abs().fillna(0) if "refund" in frame else pd.Series(0.0, index=frame.index)


@dataclass(frozen=True)
class Settlements:
    batches: pd.DataFrame  # one row per (settlement feed, payout id): BATCH_COLUMNS plus bank_pos
    credit_rows: frozenset[int]  # bank source rows naming any of the batches

    # `|` joins payouts and remittances (funnel.remittance: feed "logistics", the carrier as `channel`).

    def __or__(self, other: Settlements) -> Settlements:
        frames = [b for b in (self.batches, other.batches) if len(b)]
        return Settlements(pd.concat(frames, ignore_index=True) if frames else self.batches,
                           self.credit_rows | other.credit_rows)


def settle_batches(feeds: dict[str, pd.DataFrame], marketplaces: dict[str, Channel], policy: Policy) -> Settlements:
    """Each payout of each marketplace's settlement report, tied out against the bank credit naming it."""
    bank, rows, credit_rows = feeds["bank"], [], set()
    for feed, (channel_name, channel) in _report_channels(marketplaces).items():
        lines = feeds[feed]
        lines = lines[lines["payout_id"].notna()].assign(_fees=_fees, _refund=_refund)
        credits = payout_credits(bank, lines["payout_id"].unique())
        credit_rows |= set(bank.loc[credits["bank_pos"], SOURCE_ROW].astype(int))
        by_payout = credits.groupby("payout_id")["bank_pos"].agg(list)
        tolerance = channel.tolerance(policy)
        for payout_id, group in lines.groupby("payout_id", sort=True):
            rows.append(tie_out(bank, by_payout.get(payout_id, []), tolerance, settlement_feed=feed,
                                channel=channel_name, payout_id=payout_id, lines=len(group),
                                gross=float(group["gross"].fillna(0).sum()), fees=float(group["_fees"].sum()),
                                refund=float(group["_refund"].sum()), net=float(group["net"].fillna(0).sum())))
    return Settlements(batch_frame(rows), frozenset(credit_rows))


def batch_frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=[*BATCH_COLUMNS, "bank_pos"])


def tie_out(bank: pd.DataFrame, positions: list[int], tolerance: float, *, kind: str = "Payout",
            **batch) -> dict:
    """One batch row: `batch` (its feed, channel, payout_id, lines, gross, fees, refund and net) tied out
    against the bank credits at `positions` (the rows naming it)."""
    net = batch["net"]
    credit = bank.loc[positions[0]] if len(positions) == 1 else None
    delta = float(credit["credit_amount"]) - net if credit is not None else None
    if len(positions) > 1:
        reason = CREDIT_AMBIGUOUS
    elif credit is None:
        reason = TIE_OUT if net <= tolerance else "MISSING_PAYMENT"
    else:
        reason = TIE_OUT if abs(delta) <= tolerance + 1e-9 else BATCH_MISMATCH
    row = {**batch, "txn_ref": credit["txn_ref"] if credit is not None else None,
           "bank_source_row": int(credit[SOURCE_ROW]) if credit is not None else None,
           "credit_amount": float(credit["credit_amount"]) if credit is not None else None,
           "tie_out_delta": delta, "decision": BATCH_DECISIONS[reason], "reason_codes": reason,
           "bank_pos": positions[0] if credit is not None else None}
    return {**row, "explanation": _batch_sentence(row, len(positions), kind)}


def _batch_sentence(row: dict, credits: int, kind: str) -> str:
    batch, named = f"{kind} {row['payout_id']}", f"{kind.lower()} {row['payout_id']}"
    lines, net = row["lines"], row["net"]
    reason = row["reason_codes"]
    if reason == CREDIT_AMBIGUOUS:
        return f"{batch} is named by {credits} bank credits; a reviewer picks the one that pays it."
    if reason == "MISSING_PAYMENT":
        return f"No bank credit names {named} ({lines} lines, net {net:,.2f})."
    if row["txn_ref"] is None:
        return f"{batch} nets {net:,.2f} over {lines} lines: nothing to pay."
    if reason == TIE_OUT:
        return f"Bank credit {row['txn_ref']} equals the net of {named}'s {lines} lines."
    delta = row["tie_out_delta"]
    return (f"Bank credit {row['txn_ref']} is {abs(delta):,.2f} {'more' if delta > 0 else 'less'} than the net "
            f"of {named}'s {lines} lines ({net:,.2f}); every order in it is reviewed.")


def _lines(ctx: FunnelContext, feed: str) -> pd.DataFrame:
    """A settlement report's lines, every column prefixed `settlement_`."""
    lines = ctx.feeds[feed].drop(columns=[INGESTED_AT], errors="ignore")
    lines = lines.assign(fees=_fees(lines), refund=_refund(lines))
    return lines.add_prefix(PREFIX)


def _measure(units: pd.DataFrame, channel: Channel, ctx: FunnelContext) -> pd.DataFrame:
    """Join each unit to its line and its batch; measure gross, refund and fees against the channel's terms."""
    feed, oms = channel.settlement, ctx.feeds["oms"]
    orders = oms.loc[oms[CHANNEL].astype(str).str.strip().isin(
        [n for n, c in ctx.marketplaces.items() if c.settlement == feed]), CHANNEL_ORDER_NO] \
        if CHANNEL_ORDER_NO in oms else pd.Series(dtype=object)
    keyed = units if CHANNEL_ORDER_NO in units else units.assign(**{CHANNEL_ORDER_NO: None})
    out = link_one_to_one(keyed, orders.dropna(), _lines(ctx, feed), CHANNEL_ORDER_NO, f"{PREFIX}order_no",
                           ("", "_settlement"), "settlement_link_pass")
    out["settlement_feed"] = feed
    batches = ctx.batches[ctx.batches["settlement_feed"].eq(feed)].set_index("payout_id")
    payout = out[f"{PREFIX}payout_id"]
    out["batch_kind"], out["batch_feed"], out["batch_id"] = "payout", feed, payout
    out["batch_reason"] = payout.map(batches["reason_codes"])
    out["batch_tie_out_delta"] = payout.map(batches["tie_out_delta"])
    out["batch_bank_pos"] = payout.map(batches["bank_pos"])
    expected = out["order_amount"]
    if ITEM_TOTAL in out:
        expected = out[ITEM_TOTAL].where(out[ITEM_TOTAL].notna(), expected)
    out["settlement_gross_delta"] = out[f"{PREFIX}gross"] - expected
    out["settlement_fee_bps"] = channel.fee_bps
    if channel.fee_bps is not None:
        out["settlement_fee_delta"] = out[f"{PREFIX}fees"] - out[f"{PREFIX}gross"] * channel.fee_bps / 10_000
    tolerance = channel.tolerance(ctx.policy)
    linked = out["settlement_link_pass"].notna()
    out["_amount_off"] = linked & ((out[f"{PREFIX}refund"] > tolerance + 1e-9)
                                   | ~within(out["settlement_gross_delta"], tolerance))
    out["_fee_off"] = linked & ~within(out["settlement_fee_delta"], tolerance) \
        if channel.fee_bps is not None else False
    return out


def settle_marketplace(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
    """Decide every order of a marketplace channel along its chain (see the module docstring)."""
    if not ctx.marketplaces or CHANNEL not in units:
        return pass_on(units)
    channels = units[CHANNEL].fillna("").astype(str).str.strip()
    market, remaining = split(units, channels.isin(list(ctx.marketplaces)))
    if market.empty:
        return pass_on(units)
    market = attach_exact(market, ctx)
    names = market[CHANNEL].fillna("").astype(str).str.strip()
    measured = pd.concat([_measure(group, ctx.marketplaces[name], ctx)
                          for name, group in market.groupby(names, sort=True)], ignore_index=True)
    payout_rows = measured["batch_bank_pos"].notna()
    links = pd.DataFrame({"unit_id": measured.loc[payout_rows, "unit_id"],
                          "bank_pos": measured.loc[payout_rows, "batch_bank_pos"].astype(int)})
    measured = attach_bank(measured, bank_frame(ctx), links, "payout")

    decided, open_ = [], measured
    no_line = open_["settlement_link_pass"].isna()
    checks = [
        ("MISSING_PAYMENT", no_line & open_["requires_payment"]),
        ("AMOUNT_MISMATCH", open_["_amount_off"]),
        ("FEE_MISMATCH", open_["_fee_off"]),
        ("MISSING_PAYMENT", open_["batch_reason"].eq("MISSING_PAYMENT")),
    ]
    for reason, mask in checks:
        failed, open_ = split(open_, mask.reindex(open_.index).fillna(False).astype(bool))
        decided.append(exception(failed, reason))
    batch = open_["batch_reason"].isin([BATCH_MISMATCH, CREDIT_AMBIGUOUS])
    review, open_ = split(open_, batch)
    decided.append(decide(review, REVIEW, BATCH_REVIEW_CONFIDENCE, review["batch_reason"]))
    reasons = open_["settlement_link_pass"].notna().map({True: "SETTLEMENT_TIE_OUT",
                                                         False: "TIE_OUT_PER_STATUS_POLICY"})
    decided.append(decide(open_, AUTO_ACCEPT, TIE_OUT_CONFIDENCE, reasons))
    out = pd.concat(decided).drop(columns=["_amount_off", "_fee_off", "batch_bank_pos"])
    return StepOutcome(decided=out, remaining=remaining)


def batch_results(batches: pd.DataFrame, results: pd.DataFrame) -> pd.DataFrame:
    """The batches as exported: BATCH_COLUMNS, plus how many orders of this run each pays and which of
    those orders are exceptions on their own line."""
    columns = [*BATCH_COLUMNS, "orders", "exception_orders"]
    if batches.empty or "batch_id" not in results:
        return batches.drop(columns="bank_pos").assign(orders=0, exception_orders="").reindex(columns=columns)
    linked = results.loc[results["batch_id"].notna(), ["batch_feed", "batch_id", "order_id", "decision"]]
    keys = ["settlement_feed", "payout_id"]
    linked = linked.rename(columns={"batch_feed": "settlement_feed", "batch_id": "payout_id"})
    per_batch = linked.groupby(keys).agg(
        orders=("order_id", "size"),
        exception_orders=("order_id", lambda ids: "|".join(
            ids[linked.loc[ids.index, "decision"].eq(EXCEPTION)].astype(str))))
    out = batches.drop(columns="bank_pos").merge(per_batch.reset_index(), on=keys, how="left")
    out["orders"] = out["orders"].fillna(0).astype(int)
    out["exception_orders"] = out["exception_orders"].fillna("")
    return out[columns]
