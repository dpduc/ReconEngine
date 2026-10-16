"""Warning checks on monetary values: missing, zero and outlying amounts."""

from __future__ import annotations

import pandas as pd

from recon.checks import register, warning
from recon.config import RunConfig
from recon.health import Issue
from recon.schema import AMOUNT_COLUMNS, numeric_columns

# Amounts that should never be zero on a real order or payment (fees and COD may be).
NONZERO_COLUMNS = {feed: (AMOUNT_COLUMNS[feed],) for feed in ("oms", "bank")}
# Headline amounts screened for outliers.
OUTLIER_COLUMNS = {feed: (column,) for feed, column in AMOUNT_COLUMNS.items()}
OUTLIER_IQR_MULTIPLIER = 3.0


def _columns(feeds: dict[str, pd.DataFrame], config: RunConfig, spec: dict[str, tuple[str, ...]] | None):
    """(feed, frame, column) for the feed kind's `spec` columns present; `spec` None = its numeric columns."""
    for feed, frame in feeds.items():
        kind = config.kind(feed) or ""
        for column in numeric_columns(kind, frame.columns) if spec is None else spec.get(kind, ()):
            if column in frame.columns:
                yield feed, frame, column


@register
def missing_amount(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    issues = []
    for feed, frame, column in _columns(feeds, config, None):
        mask = frame[column].isna()
        if mask.any():
            issues.append(warning(f"missing_amount:{feed}:{column}", "missing_amount", feed, frame, mask,
                                  f"{feed}: {int(mask.sum())} rows have no '{column}'"))
    return issues


@register
def zero_amount(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    issues = []
    for feed, frame, column in _columns(feeds, config, NONZERO_COLUMNS):
        mask = frame[column].eq(0)
        if mask.any():
            issues.append(warning(f"zero_amount:{feed}:{column}", "zero_amount", feed, frame, mask,
                                  f"{feed}: {int(mask.sum())} rows have '{column}' = 0"))
    return issues


@register
def amount_outlier(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    """Amounts beyond Q1/Q3 -/+ 3 IQR. Skipped when the IQR is zero (a mostly constant column)."""
    issues = []
    for feed, frame, column in _columns(feeds, config, OUTLIER_COLUMNS):
        values = frame[column]
        q1, q3 = values.quantile([0.25, 0.75])
        iqr = q3 - q1
        if not iqr > 0:
            continue
        low, high = q1 - OUTLIER_IQR_MULTIPLIER * iqr, q3 + OUTLIER_IQR_MULTIPLIER * iqr
        mask = (values < low) | (values > high)
        if mask.any():
            issues.append(warning(f"amount_outlier:{feed}:{column}", "amount_outlier", feed, frame, mask,
                                  f"{feed}: {int(mask.sum())} '{column}' values fall outside "
                                  f"[{low:,.2f}, {high:,.2f}] (Q1/Q3 -/+ {OUTLIER_IQR_MULTIPLIER:g} IQR)"))
    return issues
