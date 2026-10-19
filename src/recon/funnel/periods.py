"""Periods and open items (ADR 0002): an order whose money is not yet due at the cut-off is OPEN.

When an order's money is due, by its channel's terms (see ``config.Channel``):

    prepaid order                              its order date
    COD or marketplace order, delivered        the first payout weekday after delivery + release_days
      with no shipment record                  its order date + settlement_days (None: not due)
      shipment not yet delivered               not due

The period's grace days are added: `due_date` is the day the money is due by. This applies wherever
money is missing because it has not arrived: not yet due at the cut-off is OPEN(NOT_YET_DUE), an
open item carried into the next period; overdue stays EXCEPTION(MISSING_PAYMENT), with its due date.
A missing shipment is held the same way while its channel's `ship_days` have not passed.
Without a period, missing money and shipments are always exceptions.
"""

from __future__ import annotations

import pandas as pd

from recon.config import Channel
from recon.funnel.base import FunnelContext, decide, split
from recon.funnel.reasons import exception
from recon.schema import CHANNEL, OPEN

OPEN_CONFIDENCE = 0.9
NOT_YET_DUE = "NOT_YET_DUE"


def _payout_day(day: pd.Timestamp, weekdays: tuple[int, ...]) -> pd.Timestamp:
    """The first day strictly after `day` on one of `weekdays`; `day` itself when any day pays."""
    if not weekdays:
        return day
    day += pd.Timedelta(days=1)
    while day.weekday() not in weekdays:
        day += pd.Timedelta(days=1)
    return day


def _logistics(units: pd.DataFrame, column: str) -> pd.Series:
    name = next((c for c in (f"{column}_logistics", column) if c in units), None)
    return units[name] if name else pd.Series(None, index=units.index, dtype=object)


def _days(ctx: FunnelContext, feed: str, values: pd.Series) -> pd.Series:
    return ctx.dates(feed, values, utc=True).dt.tz_convert(None).dt.normalize()


def due_dates(units: pd.DataFrame, ctx: FunnelContext) -> pd.Series:
    """The day each unit's money is due by (grace included); NaT when it is not due yet at all."""
    ordered = _days(ctx, "oms", units["ordered_at"])
    delivered = _days(ctx, "logistics", _logistics(units, "delivered_at"))
    shipped = units["tracking_code"].notna() if "tracking_code" in units else pd.Series(False, index=units.index)
    names = units[CHANNEL].fillna("").astype(str).str.strip() if CHANNEL in units else pd.Series("", index=units.index)
    cod = units["payment_method"].eq("COD") if "payment_method" in units else pd.Series(False, index=units.index)
    due = []
    for name, is_cod, order_day, delivery_day, has_shipment in zip(names, cod, ordered, delivered, shipped, strict=True):
        terms = ctx.channels.get(name, Channel())
        if not (is_cod or name in ctx.marketplaces):
            due.append(order_day)
        elif not pd.isna(delivery_day):
            due.append(_payout_day(delivery_day + pd.Timedelta(days=terms.release_days), terms.payout_weekdays))
        elif not has_shipment and terms.settlement_days is not None:
            due.append(order_day + pd.Timedelta(days=terms.settlement_days))
        else:
            due.append(pd.NaT)
    grace = pd.Timedelta(days=ctx.period.grace_days if ctx.period else 0)
    return pd.Series(due, index=units.index, dtype="datetime64[ns]") + grace


def ship_dates(units: pd.DataFrame, ctx: FunnelContext) -> pd.Series:
    """The day each unit's shipment is due by (grace included); NaT when its channel sets no `ship_days`."""
    ordered = _days(ctx, "oms", units["ordered_at"])
    names = units[CHANNEL].fillna("").astype(str).str.strip() if CHANNEL in units else pd.Series("", index=units.index)
    days = [ctx.channels.get(name, Channel()).ship_days for name in names]
    lead = pd.to_timedelta([pd.NaT if d is None else pd.Timedelta(days=d) for d in days]).to_numpy()
    return ordered + lead + pd.Timedelta(days=ctx.period.grace_days)


def _hold(units: pd.DataFrame, due: pd.Series, waits: pd.Series, reason: str, ctx: FunnelContext,
          waiting_for: str) -> pd.DataFrame:
    """OPEN(NOT_YET_DUE) where `waits`, else EXCEPTION(reason); both carry their due date."""
    out = units.assign(due_date=due.dt.strftime("%Y-%m-%d"), cutoff=ctx.period.cutoff, waiting_for=waiting_for)
    waiting, overdue = split(out, waits)
    return pd.concat([decide(waiting, OPEN, OPEN_CONFIDENCE, NOT_YET_DUE).assign(priority=0),
                      exception(overdue, reason)])


def missing_payment(units: pd.DataFrame, ctx: FunnelContext) -> pd.DataFrame:
    """Decide units whose money has not arrived: OPEN when not yet due at the cut-off, else
    EXCEPTION(MISSING_PAYMENT)."""
    if ctx.period is None or units.empty:
        return exception(units, "MISSING_PAYMENT")
    due = due_dates(units, ctx)
    return _hold(units, due, due.isna() | (due > pd.Timestamp(ctx.period.cutoff).normalize()), "MISSING_PAYMENT",
                 ctx, "payment")


def missing_shipment(units: pd.DataFrame, ctx: FunnelContext) -> pd.DataFrame:
    """Decide units with no shipment: OPEN while not yet due to ship at the cut-off, else
    EXCEPTION(MISSING_SHIPMENT)."""
    if ctx.period is None or units.empty:
        return exception(units, "MISSING_SHIPMENT")
    due = ship_dates(units, ctx)
    return _hold(units, due, due > pd.Timestamp(ctx.period.cutoff).normalize(), "MISSING_SHIPMENT", ctx,
                 "shipment")
