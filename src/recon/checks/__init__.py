"""Check registry. Each check takes canonical feeds and returns issues.

Add a check by decorating it with ``@register`` in a module of this package and
importing that module at the bottom of this file. Checks must tolerate feeds
that lack some canonical columns (the missing-column check reports those).
"""

from __future__ import annotations

from typing import Callable, Iterable

import pandas as pd

from recon.config import RunConfig
from recon.health import Issue

Check = Callable[[dict[str, pd.DataFrame], RunConfig], Iterable[Issue]]

CHECKS: list[Check] = []


def register(check: Check) -> Check:
    CHECKS.append(check)
    return check


def run_checks(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    return [issue for check in CHECKS for issue in check(feeds, config)]


from recon.checks import columns  # noqa: E402,F401  (registers checks)
