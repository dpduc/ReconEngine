"""Warning check: records on one side with no counterpart on the other, by exact order reference."""

from __future__ import annotations

import pandas as pd

from recon.checks import register, warning
from recon.config import RunConfig
from recon.funnel.linking import bank_order_refs
from recon.health import Issue
from recon.schema import ORDER_REF_COLUMNS


def _order_refs(feeds: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
    """Order reference per row of each feed that carries one, as linking reads it."""
    refs = {}
    for feed, column in ORDER_REF_COLUMNS.items():
        frame = feeds.get(feed)
        if frame is None:
            continue
        if feed == "bank" and ({column, "description"} & set(frame.columns)):
            refs[feed] = bank_order_refs(frame)
        elif column in frame.columns:
            refs[feed] = frame[column]
    return refs


@register
def coverage(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    refs = _order_refs(feeds)
    if "oms" not in refs:
        return []
    orders = set(refs["oms"].dropna())
    issues = []
    for feed, gap in (("bank", "no_payment"), ("logistics", "no_shipment")):
        if feed not in refs:
            continue
        mask = ~refs["oms"].isin(set(refs[feed].dropna()))
        if mask.any():
            issues.append(warning(f"coverage:oms:{gap}", "coverage", "oms", feeds["oms"], mask,
                                  f"oms: {int(mask.sum())} orders have no {feed} record"))
        orphan = ~refs[feed].isin(orders)
        if orphan.any():
            issues.append(warning(f"coverage:{feed}:no_order", "coverage", feed, feeds[feed], orphan,
                                  f"{feed}: {int(orphan.sum())} records match no order"))
    return issues
