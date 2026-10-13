"""Resolutions: the analyst's choices on Health Report issues, kept in the run config.

``resolve`` / ``undo`` edit the config; ``apply_resolutions`` replays it declaratively on the
canonical feeds. Raw files are never touched: resolutions transform the loaded frames only.
Register a handler per fix-option id in ``HANDLERS``; resolutions whose option has no feed
handler leave the feeds unchanged (acknowledgements; ``set_status_policy`` is read through
``RunConfig.resolved_policy``).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

import pandas as pd

from recon.config import Resolution, RunConfig
from recon.health import ACKNOWLEDGE, REMAP

Feeds = dict[str, pd.DataFrame]
Handler = Callable[[Feeds, Resolution, RunConfig], Feeds]

HANDLERS: dict[str, Handler] = {}


def apply_resolutions(feeds: Feeds, config: RunConfig) -> Feeds:
    for resolution in config.resolutions:
        handler = HANDLERS.get(resolution.option)
        if handler is not None:
            feeds = handler(feeds, resolution, config)
    return feeds


def resolve(config: RunConfig, issue_id: str, option: str, params: dict[str, Any] | None = None,
            note: str = "") -> RunConfig:
    """`config` with the chosen fix option for `issue_id` (replacing an earlier choice for it)."""
    if option == ACKNOWLEDGE and not note.strip():
        raise ValueError("an acknowledgement needs a note")
    kept = tuple(r for r in config.resolutions if r.issue_id != issue_id)
    return replace(config, resolutions=kept + (Resolution(issue_id, option, dict(params or {}), note.strip()),))


def undo(config: RunConfig, index: int) -> RunConfig:
    """`config` without its `index`-th resolution."""
    return replace(config, resolutions=config.resolutions[:index] + config.resolutions[index + 1:])


def remap(feeds: Feeds, resolution: Resolution, config: RunConfig) -> Feeds:
    """params: feed, canonical, source. The unmapped source column takes the canonical name."""
    feed, canonical, source = (resolution.params[k] for k in ("feed", "canonical", "source"))
    frame = feeds[feed]
    if source not in frame or canonical in frame:
        return feeds
    return {**feeds, feed: frame.rename(columns={source: canonical})}


HANDLERS[REMAP] = remap

from recon import integrity_fixes  # noqa: E402,F401  (registers handlers)
