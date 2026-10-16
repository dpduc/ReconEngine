"""Blocking integrity checks: duplicate keys, unparseable values and dates."""

from __future__ import annotations

import pandas as pd

from recon.checks import register, register_raw, sample
from recon.config import RunConfig
from recon.health import DUPLICATE_STRATEGY, EXCLUDE_ROWS, SET_DATE_FORMAT, FixOption, Issue, Severity
from recon.ingest import parse_dates
from recon.schema import DATE_COLUMNS, PRIMARY_KEYS, SOURCE_ROW, numeric_columns, review_column


def sample_rows(frame: pd.DataFrame, columns: list[str]) -> tuple[dict, ...]:
    return sample(frame[[SOURCE_ROW] + [c for c in columns if c in frame.columns]])


def exclude_option(feed: str, rows: pd.DataFrame, label: str) -> FixOption:
    return FixOption(EXCLUDE_ROWS, label,
                     {"feed": feed, "source_rows": [int(r) for r in rows[SOURCE_ROW]], "reason": ""})


def _parse_issue(feed: str, column: str, bad: pd.DataFrame, check: str, kind: str, total_rows: int,
                 extra_options: tuple[FixOption, ...] = ()) -> Issue:
    return Issue(
        id=f"{check}:{feed}:{column}",
        check=check,
        severity=Severity.BLOCKING,
        feed=feed,
        affected_rows=len(bad),
        message=f"{feed}: {len(bad)} '{column}' values are not valid {kind}",
        sample_rows=sample_rows(bad, [column]),
        fix_options=extra_options + (exclude_option(feed, bad, f"Exclude the {len(bad)} rows"),),
        total_rows=total_rows,
    )


@register_raw
def unparseable_value(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    issues = []
    for feed, frame in feeds.items():
        for column in numeric_columns(config.kind(feed) or "", frame.columns):
            values = frame[column]
            bad = frame[values.notna() & pd.to_numeric(values, errors="coerce").isna()]
            if len(bad):
                issues.append(_parse_issue(feed, column, bad, "unparseable_value", "numbers", len(frame)))
    return issues


DATE_FORMAT_CANDIDATES = tuple(
    day + time
    for day in ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y", "%Y/%m/%d", "%d.%m.%Y", "%Y%m%d")
    for time in ("", " %H:%M:%S", " %H:%M")
)


@register
def unparseable_date(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    issues = []
    for feed, frame in feeds.items():
        for column in DATE_COLUMNS.get(config.kind(feed), ()):
            if column not in frame:
                continue
            values = frame[column]
            bad = frame[values.notna() & parse_dates(values, config.date_format(feed)).isna()]
            if bad.empty:
                continue
            candidates = [fmt for fmt in DATE_FORMAT_CANDIDATES
                          if parse_dates(bad[column], fmt).notna().all()]
            option = FixOption(SET_DATE_FORMAT, f"Choose the date format of '{feed}'",
                               {"feed": feed, "date_format": candidates[0] if candidates else None,
                                "candidates": candidates})
            issues.append(_parse_issue(feed, column, bad, "unparseable_date", "dates", len(frame), (option,)))
    return issues


@register
def duplicate_key(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    issues = []
    for feed, frame in feeds.items():
        key = PRIMARY_KEYS.get(config.kind(feed))
        if key not in frame.columns:
            continue
        open_rows = frame[frame[review_column(feed)].isna()] if review_column(feed) in frame else frame
        dupes = open_rows[open_rows[key].notna() & open_rows[key].duplicated(keep=False)]
        if dupes.empty:
            continue
        issues.append(Issue(
            id=f"duplicate_key:{feed}:{key}",
            check="duplicate_key",
            severity=Severity.BLOCKING,
            feed=feed,
            affected_rows=len(dupes),
            message=f"{feed}: {dupes[key].nunique()} '{key}' values occur more than once ({len(dupes)} rows)",
            sample_rows=sample_rows(dupes.sort_values([key, SOURCE_ROW]), [key]),
            fix_options=tuple(
                FixOption(DUPLICATE_STRATEGY, label, {"feed": feed, "strategy": strategy})
                for strategy, label in (("keep_first", "Keep the first row per key"),
                                        ("keep_latest", "Keep the latest row per key"),
                                        ("review", "Route all duplicates to review"))
            ) + (exclude_option(feed, dupes, "Exclude all duplicate rows"),),
            total_rows=len(open_rows),
        ))
    return issues
