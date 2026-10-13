"""Check registry. Each check takes canonical feeds and returns issues.

Add a check by decorating it with ``@register`` in a module of this package and
importing that module at the bottom of this file. Checks must tolerate feeds
that lack some canonical columns (the missing-column check reports those).
"""

from __future__ import annotations

from typing import Callable, Iterable

import pandas as pd

from recon.config import RunConfig
from recon.health import ACKNOWLEDGE, FixOption, Issue, Severity

Check = Callable[[dict[str, pd.DataFrame], RunConfig], Iterable[Issue]]

CHECKS: list[Check] = []


def register(check: Check) -> Check:
    CHECKS.append(check)
    return check


def run_checks(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    return [issue for check in CHECKS for issue in check(feeds, config)]


SAMPLE_SIZE = 5


def warning(issue_id: str, check: str, feed: str, frame: pd.DataFrame, mask: pd.Series,
            message: str) -> Issue:
    """A warning on the rows of `frame` selected by `mask`; it must be acknowledged before `run`."""
    sample = frame[mask].head(SAMPLE_SIZE).astype(object)
    sample = sample.where(sample.notna(), None)
    return Issue(
        id=issue_id, check=check, severity=Severity.WARNING, feed=feed,
        affected_rows=int(mask.sum()), message=message,
        sample_rows=tuple(sample.to_dict("records")),
        fix_options=(FixOption(ACKNOWLEDGE, "Acknowledge with a note"),),
    )


from recon.checks import columns  # noqa: E402,F401  (registers checks)
from recon.checks import amounts, coverage, events  # noqa: E402,F401  (warning checks)
