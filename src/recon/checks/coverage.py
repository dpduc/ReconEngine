"""Warning check: records on one side with no counterpart on the other, by exact order reference.

Marketplace orders are paid through their settlement report, and COD orders of a remittance channel
through their carrier's remittance, not by a credit naming them, so they are not missing a bank
record; a bank credit naming a payout or a remittance is not missing an order.
"""

from __future__ import annotations

import re

import pandas as pd

from recon.checks import register, warning
from recon.config import RunConfig
from recon.funnel.linking import bank_order_refs
from recon.health import Issue
from recon.funnel.settlement import payout_credits
from recon.schema import CHANNEL, ORDER_REF_COLUMNS, PAYOUT_ID, REMITTANCE_ID


def _order_refs(feeds: dict[str, pd.DataFrame], config: RunConfig) -> dict[str, pd.Series]:
    """Order reference per row of each feed that carries one, as linking reads it."""
    refs = {}
    for feed, column in ORDER_REF_COLUMNS.items():
        frame = feeds.get(feed)
        if frame is None:
            continue
        if feed == "bank" and ({column, "description"} & set(frame.columns)):
            refs[feed] = bank_order_refs(frame, config.order_ref_patterns)
        elif column in frame.columns:
            refs[feed] = frame[column]
    return refs


def _paid_elsewhere(oms: pd.DataFrame, config: RunConfig) -> pd.Series:
    """Orders paid through a batch (a marketplace's payouts, a COD channel's remittances), not by a credit
    naming them."""
    if CHANNEL not in oms.columns:
        return pd.Series(False, index=oms.index)
    channels = oms[CHANNEL].fillna("").astype(str).str.strip()
    paid = channels.isin(list(config.marketplaces))
    if config.cod_channels and "payment_method" in oms.columns:
        paid |= channels.isin(list(config.cod_channels)) & oms["payment_method"].eq("COD")
    return paid


def _batch_credits(feeds: dict[str, pd.DataFrame], config: RunConfig) -> set:
    """Bank rows naming a payout or remittance, or read as a payout by a channel's `payout_pattern`."""
    bank = feeds.get("bank")
    if bank is None or "description" not in bank.columns:
        return set()
    reports = [feeds[c.settlement] for c in config.marketplaces.values() if c.settlement in feeds]
    ids = {i for frame in reports if PAYOUT_ID in frame.columns for i in frame[PAYOUT_ID].dropna()}
    shipments = feeds.get("logistics")
    if config.cod_channels and shipments is not None and REMITTANCE_ID in shipments.columns:
        ids |= set(shipments[REMITTANCE_ID].dropna().astype(str))
    credits = set(payout_credits(bank, ids)["bank_pos"]) if ids else set()
    patterns = [re.compile(c.payout_pattern) for c in config.marketplaces.values()
                if c.payout_pattern and not c.settlement]
    if patterns:
        descriptions = bank["description"].astype(str)
        credits |= {pos for pos, text in descriptions.items() if any(p.search(text) for p in patterns)}
    return credits


@register
def coverage(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    refs = _order_refs(feeds, config)
    if "oms" not in refs:
        return []
    orders, oms = set(refs["oms"].dropna()), feeds["oms"]
    paid_elsewhere, batch_credits = _paid_elsewhere(oms, config), _batch_credits(feeds, config)
    issues = []
    for feed, gap in (("bank", "no_payment"), ("logistics", "no_shipment")):
        if feed not in refs:
            continue
        mask = ~refs["oms"].isin(set(refs[feed].dropna()))
        if feed == "bank":
            mask &= ~paid_elsewhere
        if mask.any():
            issues.append(warning(f"coverage:oms:{gap}", "coverage", "oms", oms, mask,
                                  f"oms: {int(mask.sum())} orders have no {feed} record"))
        orphan = ~refs[feed].isin(orders)
        if feed == "bank":
            orphan &= ~refs[feed].index.isin(list(batch_credits))
        if orphan.any():
            issues.append(warning(f"coverage:{feed}:no_order", "coverage", feed, feeds[feed], orphan,
                                  f"{feed}: {int(orphan.sum())} records match no order"))
    return issues
