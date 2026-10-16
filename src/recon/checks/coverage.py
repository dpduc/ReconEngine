"""Warning check: records on one side with no counterpart on the other, by exact order reference.

Marketplace orders are paid through their settlement report, not by a credit naming them, so they
are not missing a bank record; a bank credit naming a payout of a report is not missing an order.
"""

from __future__ import annotations

import pandas as pd

from recon.checks import register, warning
from recon.config import RunConfig
from recon.funnel.linking import bank_order_refs
from recon.health import Issue
from recon.funnel.settlement import payout_credits
from recon.schema import CHANNEL, ORDER_REF_COLUMNS, PAYOUT_ID


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


@register
def coverage(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    refs = _order_refs(feeds, config)
    if "oms" not in refs:
        return []
    orders = set(refs["oms"].dropna())
    oms, paid_elsewhere, payouts = feeds["oms"], pd.Series(False, index=refs["oms"].index), set()
    if config.marketplaces and CHANNEL in oms.columns:
        paid_elsewhere = oms[CHANNEL].fillna("").astype(str).str.strip().isin(list(config.marketplaces))
        reports = [feeds[c.settlement] for c in config.marketplaces.values() if c.settlement in feeds]
        ids = {i for frame in reports if PAYOUT_ID in frame.columns for i in frame[PAYOUT_ID].dropna()}
        if "bank" in refs and "description" in feeds["bank"].columns:
            payouts = set(payout_credits(feeds["bank"], ids)["bank_pos"])
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
            orphan &= ~refs[feed].index.isin(list(payouts))
        if orphan.any():
            issues.append(warning(f"coverage:{feed}:no_order", "coverage", feed, feeds[feed], orphan,
                                  f"{feed}: {int(orphan.sum())} records match no order"))
    return issues
