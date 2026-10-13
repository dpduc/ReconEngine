"""Blocking integrity checks: duplicate keys, unparseable dates, mixed currencies."""

from __future__ import annotations

import pandas as pd

from recon.checks import register, register_raw
from recon.config import RunConfig
from recon.health import FixOption, Issue, Severity
from recon.schema import CURRENCY, DATE_COLUMNS, NUMERIC_COLUMNS, PRIMARY_KEYS, SOURCE_ROW, review_column

SAMPLE_SIZE = 5


def sample_rows(frame: pd.DataFrame, columns: list[str]) -> tuple[dict, ...]:
    cols = [SOURCE_ROW] + [c for c in columns if c in frame.columns]
    head = frame[cols].head(SAMPLE_SIZE).astype(object).where(frame[cols].head(SAMPLE_SIZE).notna(), None)
    return tuple(head.to_dict("records"))


def exclude_option(feed: str, rows: pd.DataFrame, label: str) -> FixOption:
    return FixOption("exclude_rows", label,
                     {"feed": feed, "source_rows": [int(r) for r in rows[SOURCE_ROW]], "reason": ""})


def _parse_issue(feed: str, column: str, bad: pd.DataFrame, check: str, kind: str,
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
    )


@register_raw
def unparseable_value(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    issues = []
    for feed, frame in feeds.items():
        for column in NUMERIC_COLUMNS.get(feed, ()):
            if column not in frame:
                continue
            values = frame[column]
            bad = frame[values.notna() & pd.to_numeric(values, errors="coerce").isna()]
            if len(bad):
                issues.append(_parse_issue(feed, column, bad, "unparseable_value", "numbers"))
    return issues


DATE_FORMAT_CANDIDATES = tuple(
    day + time
    for day in ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y", "%Y/%m/%d", "%d.%m.%Y", "%Y%m%d")
    for time in ("", " %H:%M:%S", " %H:%M")
)


def parse_dates(values: pd.Series, date_format: str | None = None) -> pd.Series:
    """ISO 8601 always parses; `date_format` (strftime) is tried for the rest. Failures are NaT."""
    parsed = pd.to_datetime(values, format="ISO8601", errors="coerce")
    if date_format:
        parsed = parsed.fillna(pd.to_datetime(values, format=date_format, errors="coerce"))
    return parsed


@register
def unparseable_date(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    issues = []
    for feed, frame in feeds.items():
        spec = config.feeds.get(feed)
        for column in DATE_COLUMNS.get(feed, ()):
            if column not in frame:
                continue
            values = frame[column]
            bad = frame[values.notna() & parse_dates(values, spec and spec.date_format).isna()]
            if bad.empty:
                continue
            candidates = [fmt for fmt in DATE_FORMAT_CANDIDATES
                          if parse_dates(bad[column], fmt).notna().all()]
            option = FixOption("set_date_format", f"Choose the date format of '{feed}'",
                               {"feed": feed, "date_format": candidates[0] if candidates else None,
                                "candidates": candidates})
            issues.append(_parse_issue(feed, column, bad, "unparseable_date", "dates", (option,)))
    return issues


@register
def mixed_currency(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    """Amounts in one feed must share one currency; nothing is converted."""
    issues = []
    for feed, frame in feeds.items():
        if CURRENCY not in frame:
            continue
        codes = frame[CURRENCY].str.strip().str.upper()
        counts = codes.value_counts()
        if len(counts) < 2:
            continue
        others = lambda keep: frame[codes.notna() & codes.ne(keep)]
        issues.append(Issue(
            id=f"mixed_currency:{feed}",
            check="mixed_currency",
            severity=Severity.BLOCKING,
            feed=feed,
            affected_rows=int(counts.iloc[1:].sum()),
            message=f"{feed}: amounts in several currencies ({', '.join(f'{c}: {n}' for c, n in counts.items())});"
                    " currencies are not converted",
            sample_rows=sample_rows(others(counts.index[0]), [CURRENCY]),
            fix_options=tuple(exclude_option(feed, others(keep), f"Keep only {keep}, exclude the other rows")
                              for keep in counts.index),
        ))
    return issues


@register
def duplicate_key(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    issues = []
    for feed, frame in feeds.items():
        key = PRIMARY_KEYS.get(feed)
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
                FixOption("duplicate_strategy", label, {"feed": feed, "strategy": strategy})
                for strategy, label in (("keep_first", "Keep the first row per key"),
                                        ("keep_latest", "Keep the latest row per key"),
                                        ("review", "Route all duplicates to review"))
            ) + (exclude_option(feed, dupes, "Exclude all duplicate rows"),),
        ))
    return issues
