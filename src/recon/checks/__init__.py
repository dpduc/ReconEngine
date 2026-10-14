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

def _registry(checks: list[Check]) -> tuple[Callable[[Check], Check], Callable[..., list[Issue]]]:
    """A (register decorator, run function) pair over `checks`."""
    def register(check: Check) -> Check:
        checks.append(check)
        return check

    def run(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
        return [issue for check in checks for issue in check(feeds, config)]

    return register, run


CHECKS: list[Check] = []
register, run_checks = _registry(CHECKS)

# Checks that need the raw strings of numeric columns, i.e. run before numeric parsing.
RAW_CHECKS: list[Check] = []
register_raw, run_raw_checks = _registry(RAW_CHECKS)

SAMPLE_SIZE = 5


def sample(frame: pd.DataFrame) -> tuple[dict, ...]:
    """The first SAMPLE_SIZE rows of `frame` as JSON-able records (missing values -> None)."""
    head = frame.head(SAMPLE_SIZE).astype(object)
    return tuple(head.where(head.notna(), None).to_dict("records"))


def warning(issue_id: str, check: str, feed: str, frame: pd.DataFrame, mask: pd.Series,
            message: str, fixes: tuple[FixOption, ...] = ()) -> Issue:
    """A warning on the rows of `frame` selected by `mask`; it must be fixed or acknowledged before `run`."""
    return Issue(
        id=issue_id, check=check, severity=Severity.WARNING, feed=feed,
        affected_rows=int(mask.sum()), message=message, sample_rows=sample(frame[mask]),
        fix_options=fixes + (FixOption(ACKNOWLEDGE, "Acknowledge with a note"),),
    )


from recon.checks import columns  # noqa: E402,F401  (registers checks)
from recon.checks import integrity  # noqa: E402,F401
from recon.checks import amounts, coverage, currency, events, policy  # noqa: E402,F401  (warning checks)
