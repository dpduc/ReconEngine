"""Blocking check: required canonical columns must be present after mapping."""

from __future__ import annotations

import pandas as pd

from recon.checks import register
from recon.config import RunConfig
from recon.health import REMAP, FixOption, Issue, Severity
from recon.schema import LINEAGE_COLUMNS, REQUIRED_COLUMNS


@register
def missing_required_column(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    issues = []
    for feed, frame in feeds.items():
        kind = config.kind(feed)
        unmapped = sorted(set(frame.columns) - set(REQUIRED_COLUMNS[kind]) - set(LINEAGE_COLUMNS))
        for column in REQUIRED_COLUMNS[kind]:
            if column in frame.columns:
                continue
            issues.append(Issue(
                id=f"missing_column:{feed}:{column}",
                check="missing_required_column",
                severity=Severity.BLOCKING,
                feed=feed,
                affected_rows=len(frame),
                message=f"{feed}: required column '{column}' is missing after mapping",
                fix_options=(FixOption(REMAP, f"Map a source column to '{column}'",
                                       {"feed": feed, "canonical": column, "candidates": unmapped}),),
            ))
    return issues
