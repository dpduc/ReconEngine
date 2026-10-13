"""Apply run-config resolutions declaratively to canonical feeds.

Raw files are never touched: resolutions transform the loaded frames only.
Register a handler per fix-option id in ``HANDLERS``; resolutions whose option
has no handler (e.g. acknowledgements) leave the feeds unchanged.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd

from recon.config import Resolution, RunConfig

Feeds = dict[str, pd.DataFrame]
Handler = Callable[[Feeds, Resolution, RunConfig], Feeds]

HANDLERS: dict[str, Handler] = {}


def apply_resolutions(feeds: Feeds, config: RunConfig) -> Feeds:
    for resolution in config.resolutions:
        handler = HANDLERS.get(resolution.option)
        if handler is not None:
            feeds = handler(feeds, resolution, config)
    return feeds


from recon import integrity_fixes  # noqa: E402,F401  (registers handlers)
