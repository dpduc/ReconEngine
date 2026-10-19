"""Building one set, or a chain of periods, from the Olist orders."""

from __future__ import annotations

import random
from dataclasses import asdict
from typing import Any

import pandas as pd

from recon.channeldata.common import (
    DATE_SHIFT, GENERATOR_VERSION, GRACE_DAYS, MARKETPLACE_SETTLEMENT_DAYS, PRIOR_DAYS, Profile, SHIP_DAYS,
    SOURCE_FILES, STATEMENT_THROUGH_DAY, _iso,
)
from recon.channeldata.seller import _Seller
from recon.channeldata.markets import Market


def build_set(olist: pd.DataFrame, market: Market, profile: Profile,
              seed: int) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    """One set's frames (keyed by file name) and its manifest; `olist` is already localized to the market."""
    _, frames, manifest = _simulate(olist, market, profile, seed)
    return frames, manifest


def _simulate(olist: pd.DataFrame, market: Market, profile: Profile, seed: int, months: int = 1,
              statement_days: int = STATEMENT_THROUGH_DAY) -> tuple[_Seller, dict[str, pd.DataFrame], dict[str, Any]]:
    """The seller after replaying `months` months of orders from the profile's month (about
    `profile.orders` a month), with its frames and manifest. The statement runs `statement_days`
    days past the last month."""
    rng = random.Random(f"{seed}:{market.code}:{profile.name}")
    start = pd.Timestamp(f"{profile.month}-01")
    next_month = start + pd.DateOffset(months=months)
    seller = market.seller(profile, rng, start, next_month + pd.Timedelta(days=statement_days),
                           market.assumptions)
    placed = olist["order_purchase_timestamp"]
    in_month = olist[(placed >= start) & (placed < next_month)]
    pool = olist[(placed >= start - pd.Timedelta(days=PRIOR_DAYS)) & (placed < next_month)]
    keep = profile.orders * months / len(in_month)
    channels, weights = list(profile.mix), list(profile.mix.values())
    unit, number = 0, rng.randint(1_000, 9_000)
    for source in pool.itertuples(index=False):
        if rng.random() >= keep:
            continue
        number += 1
        o = source._replace(order_id=f"ORD{source.order_purchase_timestamp:%Y}-{number:07d}") \
            if market.code == "vn" else source
        current = o.order_purchase_timestamp >= start
        channel = rng.choices(channels, weights)[0]
        buyer = seller.name()
        if channel in ("shopee", "tiktok"):
            oms, truth, ref = seller.marketplace(o, current, channel)
        else:
            oms, truth, ref = getattr(seller, channel)(o, current, buyer)
        if not current:
            continue
        unit += 1
        seller.rows["oms"].append({
            "order_id": o.order_id, "channel": channel, "channel_order_no": oms["channel_order_no"],
            "customer_id": o.customer_id, "customer_name": buyer, "tracking_no": oms["tracking_no"],
            "payment_method": oms["payment_method"], "payment_type": oms["payment_type"],
            "installments": oms["installments"], "currency": market.currency, "item_total": o.item_total,
            "freight_total": o.freight_total, "order_amount": oms["order_amount"], "order_status": o.order_status,
            "ordered_at": _iso(o.order_purchase_timestamp)})
        seller.rows["truth"].append({
            "unit_id": unit, "scenario": truth.scenario, "sub_kind": truth.scenario,
            "expected_outcome": truth.outcome, "expected_exception": truth.exception,
            "payment_method": oms["payment_method"], "order_id": o.order_id, "tracking_no": oms["tracking_no"],
            "bank_txn_ref": ref, "channel": channel, "source_order_id": source.order_id})
    seller.flush_payouts()
    seller.non_sales()

    frames = {
        "oms_orders.csv": pd.DataFrame(seller.rows["oms"]),
        "bank_statement.csv": pd.DataFrame(seller.rows["bank"]).sort_values(
            ["value_date", "txn_date", "txn_ref"], kind="stable"),
        "logistics_report.csv": pd.DataFrame(seller.rows["logistics"]).sort_values(
            ["picked_at", "tracking_code"], kind="stable"),
        "shopee_settlement.csv": pd.DataFrame(seller.rows["shopee"]),
        "tiktok_settlement.csv": pd.DataFrame(seller.rows["tiktok"]),
        "ground_truth.csv": pd.DataFrame(seller.rows["truth"]),
        "links.csv": pd.DataFrame(seller.rows["links"]),
    }
    oms, truth = frames["oms_orders.csv"], frames["ground_truth.csv"]
    manifest = {
        "generator": "recon.channeldata", "generator_version": GENERATOR_VERSION, "market": market.code,
        "source": "brazil ecom", "source_files": list(SOURCE_FILES), "seed": seed, "profile": asdict(profile),
        "assumptions": asdict(market.assumptions), "date_shift_days": DATE_SHIFT.days,
        "period": {"orders": [start.date().isoformat(), (next_month - pd.Timedelta(days=1)).date().isoformat()],
                   "statement": [start.date().isoformat(), (seller.end - pd.Timedelta(days=1)).date().isoformat()]},
        "currency": market.currency, "units": len(truth),
        "orders_by_channel": oms["channel"].value_counts().to_dict(),
        "scenario_counts": truth["scenario"].value_counts().to_dict(),
        "settlement_keys": {name: list(keys) for name, keys in seller.SETTLEMENT_KEYS.items()},
        "simulated": {
            "dates": f"Olist dates shifted by {DATE_SHIFT.days} days (418 weeks), so the channels exist in the period",
            "channels": "channels, payment routes, fees, payouts, carriers and settlement reports are simulated; "
                        "order timing, volume shape, statuses and the price spread are Olist's",
            "orders": "a seeded sample of the month's Olist orders across all Olist sellers",
            **market.simulated,
        },
    }
    return seller, frames, manifest


def _as_of(shipments: pd.DataFrame, paid_on: pd.Series, cutoff_end: pd.Timestamp) -> pd.DataFrame:
    """The logistics report as it stood at a cut-off (`cutoff_end`, exclusive): a parcel delivered
    later is still in transit and has collected nothing; a remittance paid later is not on it yet."""
    out = shipments.copy()
    delivered = pd.to_datetime(out["delivered_at"], errors="coerce")
    in_transit = delivered >= cutoff_end
    remitted_later = paid_on.isna() | (paid_on >= cutoff_end)
    out.loc[in_transit, ["delivered_at"]] = ""
    out.loc[in_transit, ["cod_collected", "cod_fee"]] = 0
    out.loc[in_transit | (out["status"].eq("RETURNED") & remitted_later), "status"] = "SHIPPED"
    out.loc[remitted_later, "remittance_id"] = ""
    return out


def _period_truth(truth: pd.DataFrame, cutoff: pd.Timestamp, money_day: pd.Series, first_pick: pd.Series,
                  order_day: pd.Series, marketplace: set[str], batched: set[str]) -> None:
    """Relabel `truth` in place as it stands at `cutoff`, by the terms the set's config states.

    Not yet due at the cut-off is OPEN: money or a parcel landing after it (or after the simulated
    months), an unpaid transfer or a
    parcel never shipped while still within its grace or `SHIP_DAYS`. A marketplace payout never
    made is overdue once `MARKETPLACE_SETTLEMENT_DAYS` have passed.
    """
    ids, grace = truth["order_id"], pd.Timedelta(days=GRACE_DAYS)
    placed = ids.map(order_day)
    paid_on, picked = ids.map(money_day), ids.map(first_pick).dt.normalize()
    late = (paid_on > cutoff) | (picked > cutoff)
    # A tie-out whose money or parcel lands after the simulated months is late at the last cut-off too.
    unseen = (paid_on.isna() & ~ids.isin(batched)) | (picked.isna() & ~ids.isin(marketplace))
    late |= truth["expected_outcome"].str.startswith("PASS") & unseen
    late |= truth["scenario"].eq("MISSING_SHIPMENT") & (placed + pd.Timedelta(days=SHIP_DAYS) + grace > cutoff)
    late |= truth["scenario"].eq("MISSING_PAYMENT") & (placed + grace > cutoff)
    stuck = truth["expected_outcome"].eq("OPEN") & ids.isin(marketplace) & (
        placed + pd.Timedelta(days=MARKETPLACE_SETTLEMENT_DAYS) + grace <= cutoff)
    late &= ~stuck
    columns = ["scenario", "sub_kind", "expected_outcome", "expected_exception"]
    truth.loc[late & ~truth["expected_outcome"].eq("OPEN"), columns] = [
        "PENDING_SETTLEMENT", "PENDING_SETTLEMENT", "OPEN", "NOT_YET_DUE"]
    truth.loc[stuck, columns] = ["PAYOUT_OVERDUE", "PAYOUT_OVERDUE", "EXCEPTION", "MISSING_PAYMENT"]


def build_chain(olist: pd.DataFrame, market: Market, profile: Profile, seed: int,
                periods: int = 2) -> list[tuple[dict[str, pd.DataFrame], dict[str, Any]]]:
    """One seller over `periods` consecutive months, cut into one set per period (frames, manifest).

    A period's files hold its month: its orders, the bank lines and payouts paid in it, and the
    parcels picked in it or remitted in it, as they stood at its month end (its cut-off). An order
    whose money lands after the cut-off is OPEN in that period's truth; the next period's truth
    also covers it, as the open item it carries in.
    """
    seller, frames, manifest = _simulate(olist, market, profile, seed, months=periods, statement_days=0)
    bank, links, truth = frames["bank_statement.csv"], frames["links.csv"], frames["ground_truth.csv"]
    orders, shipments = frames["oms_orders.csv"], frames["logistics_report.csv"]
    value_day = pd.to_datetime(bank.set_index("txn_ref")["value_date"])
    # The credits that pay an order: a remittance pays a COD order; a prepaid parcel only pays its shipping there.
    cod = set(orders.loc[orders["payment_method"].eq("COD"), "order_id"])
    paying = links["feed"].eq("bank") & links["order_id"].ne("") & (
        links["link_kind"].ne("cod_remittance") | links["order_id"].isin(cod))
    money_day = links[paying].assign(day=links.loc[paying, "record_key"].map(value_day)).groupby("order_id")["day"].max()
    first_pick = pd.to_datetime(shipments.groupby("client_order_code")["picked_at"].min())
    order_day = pd.to_datetime(orders.set_index("order_id")["ordered_at"]).dt.normalize()
    marketplace = set(orders.loc[orders["channel"].isin(list(market.reports)), "order_id"])
    # Orders on a payout or remittance that was made, even one netting to nothing (no bank credit).
    remitted = shipments["remittance_id"].isin(list(seller.payout_days)) & shipments["client_order_code"].isin(cod)
    batched = set(shipments.loc[remitted, "client_order_code"]) | set(
        links.loc[links["feed"].isin([name.removesuffix("_settlement.csv") for name in seller.SETTLEMENT_KEYS]),
                  "order_id"])
    paid_on = pd.Series(seller.payout_days, dtype="datetime64[ns]")
    remitted_on = pd.to_datetime(shipments["remittance_id"].map(paid_on))
    picked = pd.to_datetime(shipments["picked_at"])
    start, carried, out = pd.Timestamp(f"{profile.month}-01"), [], []
    for number in range(periods):
        low, high = start + pd.DateOffset(months=number), start + pd.DateOffset(months=number + 1)

        def in_period(days: pd.Series) -> pd.Series:
            return (days >= low) & (days < high)  # noqa: B023 -- called within this iteration

        oms = orders[in_period(pd.to_datetime(orders["ordered_at"]))]
        statement = bank[in_period(pd.to_datetime(bank["value_date"]))]
        reports = {}
        for name, (_, payout_column, _) in seller.SETTLEMENT_KEYS.items():
            report = frames[name]
            reports[name] = report[in_period(pd.to_datetime(report[payout_column].astype(str).map(paid_on)))]                 if len(report) else report
        listed = (picked < high) & ((picked >= low) | remitted_on.isna() | (remitted_on >= low))
        logistics = _as_of(shipments[listed], remitted_on[listed], high)
        units = set(oms["order_id"]) | set(carried)
        period_truth = truth[truth["order_id"].isin(units)].copy()
        _period_truth(period_truth, high - pd.Timedelta(days=1), money_day, first_pick, order_day, marketplace,
                      batched)
        period_truth["unit_id"] = range(1, len(period_truth) + 1)
        records = {"bank": set(statement["txn_ref"]), "logistics": set(logistics["tracking_code"])}
        for name, (order_column, _, _) in seller.SETTLEMENT_KEYS.items():
            records[name.removesuffix("_settlement.csv")] = set(reports[name][order_column].astype(str))                 if len(reports[name]) else set()
        kept = [key in records.get(feed, set()) for feed, key in zip(links["feed"], links["record_key"].astype(str), strict=True)]
        carried = list(period_truth.loc[period_truth["expected_outcome"].eq("OPEN"), "order_id"])
        period_frames = {**frames, "oms_orders.csv": oms, "bank_statement.csv": statement,
                         "logistics_report.csv": logistics, **reports, "ground_truth.csv": period_truth,
                         "links.csv": links[kept]}
        period_manifest = {**manifest, "chain": {"period": number + 1, "of": periods},
                           "period": {"orders": [low.date().isoformat(), (high - pd.Timedelta(days=1)).date().isoformat()],
                                      "statement": [low.date().isoformat(),
                                                    (high - pd.Timedelta(days=1)).date().isoformat()]},
                           "units": len(period_truth), "orders_by_channel": oms["channel"].value_counts().to_dict(),
                           "scenario_counts": period_truth["scenario"].value_counts().to_dict()}
        out.append((period_frames, period_manifest))
    return out
