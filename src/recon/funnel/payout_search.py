"""Marketplace payouts without a settlement report: matched by amount only, so never auto-accepted.

Without its report, a payout credit can only be matched to the channel's orders by amount: an
order's expected net is its gross (item total, else order amount) less the channel's `fee_bps`.
Beyond a handful of orders, a great many order sets add up to the same credit, and the true one
cannot be told apart. So each payout credit is a batch in REVIEW (SETTLEMENT_REPORT_MISSING)
listing its candidate orders: the channel's orders placed in the window before it. Only a small
pool is searched for the order sets whose net adds up to the credit, within stated bounds; the
sets are listed when there are at most MAX_SETS, and otherwise `search` says why there are none.

Each order of the channel that a payout credit may include is REVIEW too, listing those payouts
with a score as pass-3 candidates are: highest for the payout nearest the day the order's money is
expected (order date + the run's median days to delivery + the channel's `release_days`), raised
for a payout whose found order sets include it. An order no payout can include is missing money
(OPEN while not yet due). The channel is tied out for the run: its payout credits against the expected net of
those orders (see `channel_tie_outs`).
"""

from __future__ import annotations

import re
import time
from typing import Any

import pandas as pd

from recon.config import PAYMENT, Channel
from recon.contracts import currency_fee
from recon.funnel.base import FunnelContext, StepOutcome, decide, fee_currency, pass_on, split
from recon.funnel.decision import TIE_OUT_CONFIDENCE
from recon.funnel.linking import CANDIDATE_MAX_SCORE, attach_exact
from recon.funnel.periods import missing_payment
from recon.funnel.settlement import Settlements, batch_frame
from recon.schema import AUTO_ACCEPT, CHANNEL, ITEM_TOTAL, ORDER_STATUS, REVIEW, SOURCE_ROW

REPORT_MISSING = "SETTLEMENT_REPORT_MISSING"
MAX_POOL = 25  # candidate orders a payout's search may consider
MAX_SETS = 3  # order sets listed at most; more means the amounts cannot tell them apart
MAX_NODES = 200_000  # search steps per payout
MAX_SECONDS = 1.0  # search time per payout
WINDOW_DAYS = 45  # a payout's candidate orders were placed this many days before it, without settlement_days
REVIEW_CONFIDENCE = 0.25
DELIVERY_DAYS = 3.0  # days from order to delivery when the run has no delivered shipment to measure it


def reportless(marketplaces: dict[str, Channel]) -> dict[str, Channel]:
    """The marketplace channels with no settlement report, by OMS channel value."""
    return {name: channel for name, channel in marketplaces.items() if not channel.settlement}


def _window(channel: Channel) -> pd.Timedelta:
    return pd.Timedelta(days=channel.settlement_days if channel.settlement_days is not None else WINDOW_DAYS)


def expected_net(orders: pd.DataFrame, channel: Channel) -> pd.Series:
    """What a payout pays for each order by the channel's terms: gross less `fee_bps` (NaN without terms)."""
    gross = orders["order_amount"]
    if ITEM_TOTAL in orders:
        gross = orders[ITEM_TOTAL].where(orders[ITEM_TOTAL].notna(), gross)
    if channel.fee_bps is None:
        return pd.Series(float("nan"), index=orders.index)
    fees = [currency_fee(g, channel.fee_bps, c) if pd.notna(g) else float("nan")
            for g, c in zip(gross, fee_currency(orders), strict=True)]
    return gross - pd.Series(fees, index=orders.index, dtype=float)


def find_sets(items: list[tuple[str, float]], target: float, tolerance: float) -> tuple[list[list[str]], str]:
    """The sets of `items` (id, net) whose nets add up to `target` within `tolerance`, and how the search ended.

    The search is bounded: at most MAX_POOL items, MAX_NODES steps and MAX_SECONDS; it stops once
    more than MAX_SETS sets are found. Sets are returned only when the search finished.
    """
    if len(items) > MAX_POOL:
        return [], f"{len(items)} candidate orders: more than {MAX_POOL}, too many to search"
    ordered = sorted(items, key=lambda item: -item[1])
    rest = [0.0] * (len(ordered) + 1)
    for i in range(len(ordered) - 1, -1, -1):
        rest[i] = rest[i + 1] + ordered[i][1]
    found: list[list[str]] = []
    nodes, started = 0, time.perf_counter()
    stack: list[tuple[int, float, tuple[str, ...]]] = [(0, 0.0, ())]
    while stack:
        nodes += 1
        if nodes > MAX_NODES or time.perf_counter() - started > MAX_SECONDS:
            return [], f"search stopped at its bound ({MAX_NODES:,} steps or {MAX_SECONDS:g} s)"
        i, total, chosen = stack.pop()
        if i == len(ordered) or total + rest[i] < target - tolerance - 1e-9:
            continue
        order_id, net = ordered[i]
        stack.append((i + 1, total, chosen))  # without this order
        if total + net <= target + tolerance + 1e-9:  # with it: a set the moment it adds up
            picked = (*chosen, order_id)
            if abs(total + net - target) <= tolerance + 1e-9:
                found.append(list(picked))
                if len(found) > MAX_SETS:
                    return [], (f"more than {MAX_SETS} order sets add up to the credit: the amounts cannot tell "
                                "them apart")
            stack.append((i + 1, total + net, picked))
    if not found:
        return [], "no set of candidate orders adds up to the credit"
    return found, f"{len(found)} order set(s) add up to the credit"


def _credits(bank: pd.DataFrame, pattern: str) -> pd.DataFrame:
    """(bank_pos, payout_id) of the bank rows whose description matches a channel's `payout_pattern`."""
    compiled, rows = re.compile(pattern), []
    for pos, text in bank["description"].astype(str).items():
        match = compiled.search(text)
        if match:
            rows.append((pos, match.group("payout") if "payout" in compiled.groupindex else match.group(0)))
    return pd.DataFrame(rows, columns=["bank_pos", "payout_id"])


def _payable(orders: pd.DataFrame, ctx: FunnelContext) -> pd.Series:
    """Orders whose status requires payment (every order without a status column)."""
    if ORDER_STATUS not in orders:
        return pd.Series(True, index=orders.index)
    paid = {status for status, evidence in ctx.policy.status_policy.items() if PAYMENT in evidence}
    return orders[ORDER_STATUS].fillna("").astype(str).str.strip().isin(paid)


def search_batches(feeds: dict[str, pd.DataFrame], ctx: FunnelContext) -> Settlements:
    """One REVIEW batch per payout credit of each channel without a report, with its candidate orders
    and, when the bounded search finds a few, the order sets whose net adds up to it."""
    bank, oms, rows, claimed = feeds["bank"], feeds["oms"], [], set()
    if CHANNEL not in oms:
        return Settlements(batch_frame([]), frozenset())
    ordered_on = ctx.dates("oms", oms["ordered_at"], utc=True).dt.tz_convert(None).dt.normalize()
    channels = oms[CHANNEL].fillna("").astype(str).str.strip()
    for name, channel in reportless(ctx.marketplaces).items():
        credits = _credits(bank, channel.payout_pattern)
        orders = oms[channels.eq(name) & _payable(oms, ctx)]
        nets = expected_net(orders, channel)
        paid_on = ctx.dates("bank", bank.loc[credits["bank_pos"], "value_date"], utc=True).dt.tz_convert(None)
        tolerance = channel.tolerance(ctx.policy) * MAX_POOL
        for (pos, payout_id), day in zip(credits.itertuples(index=False, name=None), paid_on.dt.normalize(), strict=True):
            credit = bank.loc[pos]
            placed = ordered_on.loc[orders.index]
            pool = orders[(placed <= day) & (placed >= day - _window(channel))]
            if channel.fee_bps is None:
                sets, search = [], "the channel has no fee terms (fee_bps), so no order's net is known"
            else:
                sets, search = find_sets(list(zip(pool["order_id"].astype(str), nets.loc[pool.index], strict=True)),
                                         float(credit["credit_amount"]), tolerance)
            claimed.add(int(credit[SOURCE_ROW]))
            row = {"settlement_feed": None, "channel": name, "payout_id": payout_id, "txn_ref": credit["txn_ref"],
                   "bank_source_row": int(credit[SOURCE_ROW]), "credit_amount": float(credit["credit_amount"]),
                   "decision": REVIEW, "reason_codes": REPORT_MISSING,
                   "candidate_orders": "|".join(pool["order_id"].astype(str)),
                   "candidate_sets": "|".join("+".join(found) for found in sets), "search": search,
                   "bank_pos": pos, "value_day": day}
            row["explanation"] = (f"No settlement report for {name}: bank credit {credit['txn_ref']} (payout "
                                  f"{payout_id}) can only be matched by amount, against {len(pool)} candidate "
                                  f"orders; {search}. A reviewer matches it, or loads the report.")
            rows.append(row)
    batches = batch_frame(rows)
    if rows:
        batches["value_day"] = [row["value_day"] for row in rows]
    return Settlements(batches, frozenset(claimed))


def delivery_days(ctx: FunnelContext) -> float:
    """The run's median days from order to delivery, over its delivered shipments (DELIVERY_DAYS without)."""
    shipments, oms = ctx.feeds["logistics"], ctx.feeds["oms"]
    if not {"client_order_code", "delivered_at"} <= set(shipments.columns):
        return DELIVERY_DAYS
    pairs = oms[["order_id", "ordered_at"]].merge(shipments[["client_order_code", "delivered_at"]],
                                                  left_on="order_id", right_on="client_order_code")
    days = (ctx.dates("logistics", pairs["delivered_at"], utc=True)
            - ctx.dates("oms", pairs["ordered_at"], utc=True)).dt.total_seconds() / 86_400
    return float(days.median()) if days.notna().any() else DELIVERY_DAYS


def _payout_candidates(units: pd.DataFrame, batches: pd.DataFrame, ctx: FunnelContext) -> pd.Series:
    """Per unit, the payouts that may include it ('txn_ref:score|...', best first)."""
    ordered_on = ctx.dates("oms", units["ordered_at"], utc=True).dt.tz_convert(None).dt.normalize()
    delivery = pd.Timedelta(days=delivery_days(ctx))
    listed = []
    for name, order_id, day in zip(units[CHANNEL].fillna("").astype(str).str.strip(), units["order_id"].astype(str),
                                   ordered_on, strict=True):
        channel = ctx.marketplaces.get(name)
        mine = batches[batches["channel"].eq(name)] if channel is not None else batches.iloc[0:0]
        mine = mine[(mine["value_day"] >= day) & (mine["value_day"] <= day + _window(channel or Channel()))]
        candidates = [(ref, paid, sets) for ref, paid, orders, sets in zip(
            mine["txn_ref"], mine["value_day"], mine["candidate_orders"], mine["candidate_sets"], strict=True)
                      if order_id in str(orders).split("|")]
        expected = day + delivery + pd.Timedelta(days=(channel or Channel()).release_days)
        span = _window(channel or Channel()).days + 1
        scored = []
        for ref, paid, sets in candidates:
            found = [s.split("+") for s in str(sets).split("|") if s] if isinstance(sets, str) else []
            share = sum(order_id in s for s in found) / len(found) if found else 0.0
            near = 1 - min(abs((paid - expected).days), span) / span
            scored.append((ref, round(CANDIDATE_MAX_SCORE * max(near / len(candidates), share), 4)))
        scored.sort(key=lambda item: (-item[1], item[0]))
        listed.append("|".join(f"{ref}:{score:.4f}" for ref, score in scored) or None)
    return pd.Series(listed, index=units.index, dtype=object)


def settle_reportless(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
    """Decide every order of a marketplace channel without a settlement report (see the module docstring)."""
    names = reportless(ctx.marketplaces)
    if not names or CHANNEL not in units:
        return pass_on(units)
    market, remaining = split(units, units[CHANNEL].fillna("").astype(str).str.strip().isin(list(names)))
    if market.empty:
        return pass_on(units)
    batches = ctx.batches[ctx.batches["reason_codes"].eq(REPORT_MISSING)] if ctx.batches is not None else None
    market = attach_exact(market, ctx)
    market["payout_candidates"] = _payout_candidates(market, batches, ctx) if batches is not None and len(batches) \
        else None
    free, payable = split(market, ~market["requires_payment"].astype(bool))
    listed, unlisted = split(payable, payable["payout_candidates"].notna())
    best = listed["payout_candidates"].str.split("|").str[0].str.rsplit(":", n=1).str[1].astype(float)
    count = listed["payout_candidates"].str.count(r"\|") + 1
    decided = [decide(free, AUTO_ACCEPT, TIE_OUT_CONFIDENCE, "TIE_OUT_PER_STATUS_POLICY"),
               decide(listed, REVIEW, (best / count).round(4).clip(upper=REVIEW_CONFIDENCE), REPORT_MISSING),
               missing_payment(unlisted, ctx)]
    return StepOutcome(decided=pd.concat(decided), remaining=remaining)


def channel_tie_outs(results: pd.DataFrame, batches: pd.DataFrame, ctx: FunnelContext) -> dict[str, dict[str, Any]]:
    """Per channel without a report: its payout credits in the run against the expected net of the
    orders they may include (those in REVIEW), and of the orders no credit can include yet."""
    out = {}
    for name, channel in reportless(ctx.marketplaces).items():
        mine = results[results[CHANNEL].fillna("").astype(str).str.strip().eq(name)] if CHANNEL in results \
            else results.iloc[0:0]
        nets = expected_net(mine, channel)
        reviewed = mine["reason_codes"].eq(REPORT_MISSING)
        credits = batches[batches["channel"].eq(name) & batches["reason_codes"].eq(REPORT_MISSING)]
        credited, expected = float(credits["credit_amount"].sum()), float(nets[reviewed].sum())
        out[name] = {"payout_credits": int(len(credits)), "credited": round(credited, 2),
                     "orders_in_review": int(reviewed.sum()), "expected_net": round(expected, 2),
                     "difference": round(credited - expected, 2),
                     "orders_not_covered": int((~reviewed).sum()), "expected_net_not_covered":
                         round(float(nets[~reviewed].sum()), 2)}
    return out
