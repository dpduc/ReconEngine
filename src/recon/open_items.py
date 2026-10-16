"""Open items carried between periods (ADR 0002).

A run's open items (its OPEN units) are exported as their canonical OMS rows plus the day the
money is due by and the cut-off they were open at. The next run takes that file as an input: its
orders join the run's own orders, so credits for them link to them instead of becoming orphans.
"""

from __future__ import annotations

import hashlib
import io
from typing import Any

import numpy as np
import pandas as pd

from recon.ingest import parse_numeric
from recon.schema import (
    AUTO_ACCEPT,
    EXCEPTION,
    INGESTED_AT,
    LINEAGE_COLUMNS,
    OPEN,
    REVIEW,
    ROW_HASH,
    SOURCE_FILE,
    SOURCE_ROW,
)

CARRIED, CARRIED_FROM = "carried_forward", "carried_from"  # on carried units: True, and the cut-off they left


def open_items(results: pd.DataFrame, oms: pd.DataFrame) -> pd.DataFrame:
    """The OMS rows of the OPEN units, without lineage, with `due_date` and `cutoff`."""
    columns = [c for c in oms.columns if c not in (*LINEAGE_COLUMNS, CARRIED, CARRIED_FROM)]
    if results.empty or "due_date" not in results:  # no period, or nothing open
        return pd.DataFrame(columns=[*columns, "due_date", "cutoff"])
    waiting = results.loc[results["decision"].eq(OPEN), ["order_id", "due_date", "cutoff"]]
    rows = oms[columns].drop_duplicates("order_id").merge(waiting, on="order_id", how="inner")
    return rows.reset_index(drop=True)


def carry_in(oms: pd.DataFrame, data: bytes, path: str, ingested_at: str) -> pd.DataFrame:
    """`oms` with the previous run's open items (`data`, an open_items.csv) appended as orders.

    An open item whose order is also in this run's OMS is dropped: the OMS row wins.
    """
    raw = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False)
    carried = raw.replace("", np.nan).rename(columns={"cutoff": CARRIED_FROM}).drop(columns="due_date",
                                                                                   errors="ignore")
    carried[SOURCE_FILE] = path
    carried[SOURCE_ROW] = np.arange(1, len(carried) + 1)
    carried[ROW_HASH] = [hashlib.sha256("\x1f".join(row).encode("utf-8")).hexdigest()
                         for row in raw.itertuples(index=False, name=None)]
    carried[INGESTED_AT] = ingested_at
    carried = parse_numeric("oms", carried[~carried["order_id"].isin(oms["order_id"])])
    return pd.concat([oms.assign(**{CARRIED: False}), carried.assign(**{CARRIED: True})], ignore_index=True)


def open_item_counts(results: pd.DataFrame) -> dict[str, Any]:
    """Open items out of this run, and what became of those carried in: cleared (auto-accepted),
    still open, or now in the queue (REVIEW or EXCEPTION)."""
    decision = results["decision"]
    carried = results[CARRIED].fillna(False).astype(bool) if CARRIED in results else pd.Series(False,
                                                                                               index=results.index)
    return {"open": int(decision.eq(OPEN).sum()), "carried_in": int(carried.sum()),
            "cleared": int((carried & decision.eq(AUTO_ACCEPT)).sum()),
            "still_open": int((carried & decision.eq(OPEN)).sum()),
            "queued": int((carried & decision.isin([REVIEW, EXCEPTION])).sum())}
