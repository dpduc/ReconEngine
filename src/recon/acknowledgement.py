"""Warnings are accepted by an explicit "acknowledge" resolution carrying a note."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace

from recon.config import Resolution, RunConfig
from recon.health import ACKNOWLEDGE, HealthReport, Severity


def acknowledgements(config: RunConfig) -> dict[str, str]:
    """`{issue_id: note}` acknowledged in `config`; an acknowledgement without a note does not count."""
    return {r.issue_id: r.note for r in config.resolutions if r.option == ACKNOWLEDGE and r.note.strip()}


def acknowledge_warnings(config: RunConfig, report: HealthReport, note: str,
                         issue_ids: Collection[str] | None = None) -> RunConfig:
    """`config` plus an acknowledgement for every still-open warning in `report` (or only those in `issue_ids`)."""
    if not note.strip():
        raise ValueError("an acknowledgement needs a note")
    done = acknowledgements(config)
    new = tuple(Resolution(issue.id, ACKNOWLEDGE, note=note)
                for issue in report.by_severity(Severity.WARNING)
                if issue.id not in done and (issue_ids is None or issue.id in issue_ids))
    return replace(config, resolutions=config.resolutions + new)
