"""Warning checks on event timestamps: impossible ordering and placeholder times."""

from __future__ import annotations

import pandas as pd

from recon.checks import register, warning
from recon.config import RunConfig
from recon.health import Issue
from recon.ingest import parse_dates
from recon.schema import ORDER_STATUS, TIMESTAMP_COLUMNS

# The status column that splits each feed into segments.
SEGMENT_COLUMNS = {"oms": ORDER_STATUS, "logistics": "status"}
MIN_SEGMENT_ROWS = 2  # a lone midnight value is not evidence of a placeholder


def _timestamps(series: pd.Series, config: RunConfig, feed: str) -> pd.Series:
    return parse_dates(series, config.date_format(feed))


@register
def picked_after_delivered(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    frame = feeds.get("logistics")
    if frame is None or not {"picked_at", "delivered_at"} <= set(frame.columns):
        return []
    mask = _timestamps(frame["picked_at"], config, "logistics") > _timestamps(frame["delivered_at"], config, "logistics")
    if not mask.any():
        return []
    return [warning("picked_after_delivered:logistics", "picked_after_delivered", "logistics", frame, mask,
                    f"logistics: {int(mask.sum())} shipments were picked after they were delivered")]


def _all_midnight(stamps: pd.Series) -> bool:
    present = stamps.dropna()
    return len(present) >= MIN_SEGMENT_ROWS and bool(present.eq(present.dt.normalize()).all())


@register
def placeholder_timestamp(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    """A column, or a status segment of it, whose every timestamp sits at 00:00:00."""
    issues = []
    for feed, frame in feeds.items():
        for column in TIMESTAMP_COLUMNS.get(config.kind(feed), ()):
            if column not in frame.columns:
                continue
            stamps = _timestamps(frame[column], config, feed)
            if _all_midnight(stamps):
                issues.append(warning(f"placeholder_timestamp:{feed}:{column}", "placeholder_timestamp", feed,
                                      frame, stamps.notna(),
                                      f"{feed}: every '{column}' value is at 00:00:00"))
                continue
            segment_column = SEGMENT_COLUMNS.get(feed)
            if segment_column not in frame.columns:
                continue
            for segment, rows in stamps.groupby(frame[segment_column], sort=True):
                if _all_midnight(rows):
                    mask = stamps.notna() & frame[segment_column].eq(segment)
                    issues.append(warning(
                        f"placeholder_timestamp:{feed}:{column}:{segment_column}={segment}",
                        "placeholder_timestamp", feed, frame, mask,
                        f"{feed}: every '{column}' value with {segment_column}={segment} is at 00:00:00"))
    return issues
