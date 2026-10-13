"""Turn the analyst's choice on a Health Report issue into run-config resolutions.

Every choice is a ``Resolution`` in the config, so the downloaded config replays it.
A remap also writes the chosen source column into the mapping, where ``inspect`` reads it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from recon.acknowledgement import acknowledge_warnings
from recon.config import Resolution, RunConfig
from recon.health import ACKNOWLEDGE, HealthReport

REMAP = "remap"


def resolve(config: RunConfig, issue_id: str, option: str, params: dict[str, Any], note: str = "") -> RunConfig:
    """`config` with the chosen fix option for `issue_id` (replacing an earlier choice for it)."""
    if option == ACKNOWLEDGE and not note.strip():
        raise ValueError("an acknowledgement needs a note")
    mapping = config.mapping
    if option == REMAP:
        feed = params["feed"]
        mapping = {**mapping, feed: {**mapping.get(feed, {}), params["canonical"]: params["source"]}}
    kept = tuple(r for r in config.resolutions if r.issue_id != issue_id)
    return replace(config, mapping=mapping,
                   resolutions=kept + (Resolution(issue_id, option, dict(params), note.strip()),))


def undo(config: RunConfig, index: int) -> RunConfig:
    """`config` without its `index`-th resolution; undoing a remap unmaps the column again."""
    removed = config.resolutions[index]
    mapping = config.mapping
    if removed.option == REMAP:
        feed = removed.params["feed"]
        mapping = {**mapping, feed: {c: s for c, s in mapping.get(feed, {}).items()
                                     if c != removed.params["canonical"]}}
    return replace(config, mapping=mapping,
                   resolutions=config.resolutions[:index] + config.resolutions[index + 1:])


def acknowledge_all(config: RunConfig, report: HealthReport, note: str) -> RunConfig:
    return acknowledge_warnings(config, report, note.strip())
