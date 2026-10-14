"""Units carrying a row that a resolution routed to review (e.g. duplicate keys) go to REVIEW."""

from __future__ import annotations

import pandas as pd

from recon.funnel.base import FunnelContext, StepOutcome, decide, pass_on, split
from recon.schema import FEEDS, REVIEW, review_column

ROUTED_CONFIDENCE = 0.5


def route_review(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
    columns = [review_column(feed) for feed in FEEDS if review_column(feed) in units]
    if not columns:
        return pass_on(units)
    flags = units[columns]
    routed, remaining = split(units, flags.notna().any(axis=1))
    reasons = flags.loc[routed.index].apply(lambda row: "|".join(row.dropna().unique()), axis=1)
    return StepOutcome(decided=decide(routed, REVIEW, ROUTED_CONFIDENCE, reasons), remaining=remaining)
