"""Recon Run: everything one ``run`` call produces."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from recon.health import HealthReport, Issue

COMPLETED, BLOCKED = "completed", "blocked"
EXCLUDED_COLUMNS = ("feed", "source_file", "source_row", "row_hash", "issue_id", "reason")

# Per record type: (reference column, amount-at-risk column, event-date columns in preference order).
_QUEUE_FIELDS = {
    "oms": ("order_id", "order_amount", ("ordered_at",)),
    "bank": ("txn_ref", "credit_amount", ("value_date", "txn_date")),
    "logistics": ("tracking_code", "cod_collected", ("picked_at", "delivered_at")),
}


def _first_present(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.Series:
    out = pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns]")
    for column in columns:
        if column in frame:
            out = out.fillna(pd.to_datetime(frame[column], errors="coerce", format="mixed"))
    return out


def exception_queue(results: pd.DataFrame, orphans: pd.DataFrame) -> pd.DataFrame:
    """Unit exceptions plus orphans, ranked by amount at risk (desc) then age (oldest first).

    Age is measured in days back from the latest event date in the queue, so runs are reproducible.
    """
    parts = []
    if "decision" in results:
        parts.append(results[results["decision"] == "EXCEPTION"].assign(record_type="oms"))
    if len(orphans):
        parts.append(orphans.assign(record_type=orphans["orphan_feed"]))
    if not parts:
        return pd.DataFrame(columns=["record_type", "record_ref", "amount_at_risk", "age_days"])
    framed = []
    for part in parts:
        for record_type, group in part.groupby("record_type", sort=False):
            ref, amount, dates = _QUEUE_FIELDS[record_type]
            framed.append(group.assign(
                record_ref=group[ref],
                amount_at_risk=pd.to_numeric(group[amount], errors="coerce").fillna(0.0).abs(),
                _event_at=_first_present(group, dates),
            ))
    queue = pd.concat(framed, ignore_index=True)
    as_of = queue["_event_at"].max()
    queue["age_days"] = (as_of - queue["_event_at"]).dt.days
    queue = queue.sort_values(["amount_at_risk", "age_days"], ascending=[False, False],
                              na_position="last", kind="stable").drop(columns="_event_at")
    front = ["record_type", "record_ref", "amount_at_risk", "age_days", "decision", "reason_codes"]
    return queue[front + [c for c in queue.columns if c not in front]].reset_index(drop=True)


@dataclass(frozen=True)
class ReconRun:
    status: str  # COMPLETED or BLOCKED
    health: HealthReport
    results: pd.DataFrame = field(default_factory=pd.DataFrame)  # one row per unit
    funnel: pd.DataFrame = field(default_factory=pd.DataFrame)  # step, units_in, decided, passed_on
    manifest: dict[str, Any] = field(default_factory=dict)
    feeds: dict[str, pd.DataFrame] = field(default_factory=dict)  # canonical feeds the run used
    orphans: pd.DataFrame = field(default_factory=pd.DataFrame)  # unlinked bank/logistics records
    # rows a resolution took out of the run, with issue id and reason (EXCLUDED_COLUMNS)
    excluded: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=list(EXCLUDED_COLUMNS)))

    @property
    def blocked(self) -> bool:
        return self.status == BLOCKED

    @property
    def open_issues(self) -> list[Issue]:
        return self.health.open_issues if self.blocked else []

    @property
    def exception_queue(self) -> pd.DataFrame:
        return exception_queue(self.results, self.orphans)

    def bundle(self) -> dict[str, str]:
        """Downloadable artifacts keyed by file name."""
        return {
            "decisions.csv": self.results.to_csv(index=False),
            "triage_queue.csv": self.results[self.results["decision"] == "REVIEW"].to_csv(index=False),
            "exception_queue.csv": self.exception_queue.to_csv(index=False),
            "orphans.csv": self.orphans.to_csv(index=False),
            "funnel.csv": self.funnel.to_csv(index=False),
            "excluded.csv": self.excluded.to_csv(index=False),
            "manifest.json": json.dumps(self.manifest, indent=2, default=str),
        }
