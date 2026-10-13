"""Warning check: records on one side with no counterpart on the other, by exact order reference."""

from __future__ import annotations

import pandas as pd

from recon.checks import register, warning
from recon.config import RunConfig
from recon.funnel.linking import extract_order_ref
from recon.health import Issue


def _order_refs(feeds: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
    """Order reference per row of each feed that carries one."""
    refs = {}
    if "order_id" in feeds.get("oms", {}):
        refs["oms"] = feeds["oms"]["order_id"]
    if "description" in feeds.get("bank", {}):
        refs["bank"] = feeds["bank"]["description"].map(extract_order_ref)
    if "client_order_code" in feeds.get("logistics", {}):
        refs["logistics"] = feeds["logistics"]["client_order_code"]
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
