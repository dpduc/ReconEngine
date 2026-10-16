"""Recon Run: everything one ``run`` call produces."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from recon.health import HealthReport, Issue
from recon.ingest import parse_dates
from recon.schema import AMOUNT_COLUMNS, AUTO_ACCEPT, DATE_COLUMNS, EXCEPTION, PRIMARY_KEYS, REVIEW

COMPLETED, BLOCKED = "completed", "blocked"
EXCLUDED_COLUMNS = ("feed", "source_file", "source_row", "row_hash", "issue_id", "reason")


def _first_present(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.Series:
    """Per row, the first of `columns` (in order) holding a date."""
    out = pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns]")
    for column in columns:
        if column in frame:
            out = out.fillna(parse_dates(frame[column]))
    return out


def exception_queue(results: pd.DataFrame, orphans: pd.DataFrame) -> pd.DataFrame:
    """The work queue: EXCEPTION and REVIEW units plus orphans, ranked by amount at risk (desc)
    then age (oldest first). `decision` tells exceptions from units awaiting review.

    Age is measured in days back from the latest event date in the queue, so runs are reproducible.
    """
    parts = []
    if "decision" in results:
        parts.append(results[results["decision"].isin([EXCEPTION, REVIEW])].assign(record_type="oms"))
    if len(orphans):
        parts.append(orphans.assign(record_type=orphans["orphan_feed"]))
    front = ["record_type", "record_ref", "amount_at_risk", "age_days", "decision", "reason_codes"]
    framed = []
    for part in parts:
        for record_type, group in part.groupby("record_type", sort=False):
            framed.append(group.assign(
                record_ref=group[PRIMARY_KEYS[record_type]],
                amount_at_risk=pd.to_numeric(group[AMOUNT_COLUMNS[record_type]], errors="coerce").fillna(0.0).abs(),
                _event_at=_first_present(group, DATE_COLUMNS[record_type]),
            ))
    if not framed:  # nothing to queue: keep the columns so callers can still filter
        return pd.DataFrame(columns=front + [c for c in results.columns if c not in front])
    queue = pd.concat(framed, ignore_index=True)
    as_of = queue["_event_at"].max()
    queue["age_days"] = (as_of - queue["_event_at"]).dt.days
    queue = queue.sort_values(["amount_at_risk", "age_days"], ascending=[False, False],
                              na_position="last", kind="stable").drop(columns="_event_at")
    return queue[front + [c for c in queue.columns if c not in front]].reset_index(drop=True)


# Auto-accepted with a note: the first records to revisit if the period does not close.
NOTED_CODES = ("INSTALLMENT_INTEREST",)


def close_watchlist(results: pd.DataFrame) -> pd.DataFrame:
    """Auto-accepted units carrying a note, largest noted amount first, with that amount as `note_amount`."""
    if results.empty or "reason_codes" not in results:
        return pd.DataFrame(columns=["order_id", "reason_codes", "note_amount"])
    has_note = results["reason_codes"].astype(str).str.split("|").map(lambda codes: bool(set(codes) & set(NOTED_CODES)))
    noted = results[results["decision"].eq(AUTO_ACCEPT) & has_note]
    noted = noted.assign(note_amount=pd.to_numeric(noted["credit_delta"], errors="coerce").round(2))
    front = ["order_id", "reason_codes", "note_amount", "explanation"]
    ordered = noted.sort_values(["note_amount", "order_id"], ascending=[False, True], kind="stable")
    columns = [c for c in front if c in ordered] + [c for c in ordered.columns if c not in front]
    return ordered[columns].reset_index(drop=True)


def watchlist_summary(results: pd.DataFrame) -> dict[str, Any]:
    watchlist = close_watchlist(results)
    return {"units": int(len(watchlist)), "amount": round(float(watchlist["note_amount"].sum()), 2)}


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
    settlement_batches: pd.DataFrame = field(default_factory=pd.DataFrame)  # one row per payout or remittance
    # OPEN units as OMS rows with their due date and cut-off: the next run's `open_items` input
    open_items: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def blocked(self) -> bool:
        return self.status == BLOCKED

    @property
    def open_issues(self) -> list[Issue]:
        return self.health.open_issues if self.blocked else []

    @property
    def exception_queue(self) -> pd.DataFrame:
        return exception_queue(self.results, self.orphans)

    @property
    def close_watchlist(self) -> pd.DataFrame:
        return close_watchlist(self.results)

    def bundle(self) -> dict[str, str]:
        """Downloadable artifacts keyed by file name.

        Records the export in the manifest's `performance`: an `export` stage and `output_bytes`,
        the size of every artifact but the manifest itself (which carries the number).
        """
        started = time.perf_counter()
        files = {
            "decisions.csv": self.results.to_csv(index=False),
            "triage_queue.csv": self.results[self.results["decision"] == REVIEW].to_csv(index=False),
            "exception_queue.csv": self.exception_queue.to_csv(index=False),
            "orphans.csv": self.orphans.to_csv(index=False),
            "close_watchlist.csv": self.close_watchlist.to_csv(index=False),
            "funnel.csv": self.funnel.to_csv(index=False),
            "excluded.csv": self.excluded.to_csv(index=False),
            "settlement_batches.csv": self.settlement_batches.to_csv(index=False),
            "open_items.csv": self.open_items.to_csv(index=False),
        }
        performance = self.manifest.get("performance")
        if performance is not None:
            performance["stages"]["export"] = round(time.perf_counter() - started, 6)
            performance["output_bytes"] = sum(len(text.encode("utf-8")) for text in files.values())
        files["manifest.json"] = json.dumps(self.manifest, indent=2, default=str)
        return files
