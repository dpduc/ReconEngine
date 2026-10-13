"""Resolution handlers for the integrity checks: duplicate strategy, row exclusion, date format.

Rows are never dropped here: excluded rows are marked and the pipeline reports them.
"""

from __future__ import annotations

import pandas as pd

from recon.ingest import parse_dates
from recon.config import Resolution, RunConfig
from recon.health import DUPLICATE_STRATEGY, EXCLUDE_ROWS, SET_DATE_FORMAT
from recon.resolutions import HANDLERS, Feeds
from recon.schema import (
    DATE_COLUMNS,
    EXCLUDED_BY,
    EXCLUSION_REASON,
    PRIMARY_KEYS,
    SOURCE_ROW,
    review_column,
)


def _mark_excluded(frame: pd.DataFrame, mask: pd.Series, resolution: Resolution, reason: str) -> pd.DataFrame:
    frame = frame.copy()
    for column in (EXCLUDED_BY, EXCLUSION_REASON):
        if column not in frame:
            frame[column] = pd.Series(pd.NA, index=frame.index, dtype=object)
    mask = mask & frame[EXCLUDED_BY].isna()
    frame.loc[mask, EXCLUDED_BY] = resolution.issue_id
    frame.loc[mask, EXCLUSION_REASON] = reason
    return frame


def _with_frame(feeds: Feeds, feed: str, frame: pd.DataFrame) -> Feeds:
    return {**feeds, feed: frame}


def exclude_rows(feeds: Feeds, resolution: Resolution, config: RunConfig) -> Feeds:
    """params: feed, source_rows, reason (defaults to the note)."""
    feed = resolution.params["feed"]
    frame = feeds[feed]
    mask = frame[SOURCE_ROW].isin(resolution.params["source_rows"])
    reason = resolution.params.get("reason") or resolution.note or "excluded by analyst"
    return _with_frame(feeds, feed, _mark_excluded(frame, mask, resolution, reason))


def _latest_order(frame: pd.DataFrame, feed: str, config: RunConfig) -> pd.Series:
    """Sort key: the feed's own date when present, then file order."""
    for column in DATE_COLUMNS.get(feed, ()):
        if column in frame:
            return parse_dates(frame[column], config.date_format(feed))
    return frame[SOURCE_ROW]


def duplicate_strategy(feeds: Feeds, resolution: Resolution, config: RunConfig) -> Feeds:
    """params: feed, strategy in keep_first | keep_latest | review."""
    feed, strategy = resolution.params["feed"], resolution.params["strategy"]
    frame, key = feeds[feed], PRIMARY_KEYS[feed]
    live = frame[EXCLUDED_BY].isna() if EXCLUDED_BY in frame else pd.Series(True, index=frame.index)
    dupes = live & frame[key].notna() & frame[key].where(live).duplicated(keep=False)
    if strategy == "review":
        frame = frame.copy()
        frame[review_column(feed)] = pd.Series(pd.NA, index=frame.index, dtype=object).where(
            ~dupes, f"DUPLICATE_{feed.upper()}_KEY")
        return _with_frame(feeds, feed, frame)
    if strategy == "keep_first":
        order = frame[SOURCE_ROW]
    elif strategy == "keep_latest":
        order = _latest_order(frame, feed, config)
    else:
        raise ValueError(f"unknown duplicate strategy {strategy!r}")
    ranked = frame[dupes].assign(_order=order[dupes]).sort_values(["_order", SOURCE_ROW], kind="stable")
    keep = ranked.index[~ranked[key].duplicated(keep="last" if strategy == "keep_latest" else "first")]
    mask = dupes & ~frame.index.isin(keep)
    reason = f"duplicate {key} ({strategy})"
    return _with_frame(feeds, feed, _mark_excluded(frame, mask, resolution, reason))


def set_date_format(feeds: Feeds, resolution: Resolution, config: RunConfig) -> Feeds:
    """params: feed, date_format. Values that parse are rewritten as ISO 8601; the rest stay as-is."""
    feed, date_format = resolution.params["feed"], resolution.params["date_format"]
    iso = "%Y-%m-%dT%H:%M:%S" if "%H" in date_format else "%Y-%m-%d"
    frame = feeds[feed].copy()
    for column in DATE_COLUMNS.get(feed, ()):
        if column in frame:
            parsed = parse_dates(frame[column], date_format)
            frame[column] = parsed.dt.strftime(iso).where(parsed.notna(), frame[column])
    return _with_frame(feeds, feed, frame)


HANDLERS.update({
    EXCLUDE_ROWS: exclude_rows,
    DUPLICATE_STRATEGY: duplicate_strategy,
    SET_DATE_FORMAT: set_date_format,
})
