"""Recon Run: everything one ``run`` call produces."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from recon.health import HealthReport, Issue

COMPLETED, BLOCKED = "completed", "blocked"
EXCLUDED_COLUMNS = ("feed", "source_file", "source_row", "row_hash", "issue_id", "reason")


@dataclass(frozen=True)
class ReconRun:
    status: str  # COMPLETED or BLOCKED
    health: HealthReport
    results: pd.DataFrame = field(default_factory=pd.DataFrame)  # one row per unit
    funnel: pd.DataFrame = field(default_factory=pd.DataFrame)  # step, units_in, decided, passed_on
    manifest: dict[str, Any] = field(default_factory=dict)
    feeds: dict[str, pd.DataFrame] = field(default_factory=dict)  # canonical feeds the run used
    # rows a resolution took out of the run, with issue id and reason (EXCLUDED_COLUMNS)
    excluded: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=list(EXCLUDED_COLUMNS)))

    @property
    def blocked(self) -> bool:
        return self.status == BLOCKED

    @property
    def open_issues(self) -> list[Issue]:
        return self.health.open_issues if self.blocked else []

    def bundle(self) -> dict[str, str]:
        """Downloadable artifacts keyed by file name."""
        return {
            "decisions.csv": self.results.to_csv(index=False),
            "triage_queue.csv": self.results[self.results["decision"] == "REVIEW"].to_csv(index=False),
            "funnel.csv": self.funnel.to_csv(index=False),
            "excluded.csv": self.excluded.to_csv(index=False),
            "manifest.json": json.dumps(self.manifest, indent=2, default=str),
        }
