"""Funnel scaffold: steps share one interface and each unit is decided exactly once.

A step takes the units no earlier step decided and returns ``StepOutcome``:
``decided`` (rows stamped via ``decide``) and ``remaining`` (passed on, possibly
enriched with new columns). The runner records the deciding step and counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

from recon.config import Policy
from recon.schema import INGESTED_AT, LINEAGE_COLUMNS

RULE_VERSION = "1.0.0"  # bump whenever funnel logic changes

HASH_COLUMNS = ("oms_row_hash", "bank_row_hash", "logistics_row_hash")


@dataclass(frozen=True)
class FunnelContext:
    feeds: dict[str, pd.DataFrame]  # canonical feeds after resolutions
    policy: Policy


@dataclass(frozen=True)
class StepOutcome:
    decided: pd.DataFrame
    remaining: pd.DataFrame


@dataclass(frozen=True)
class FunnelStep:
    name: str
    apply: Callable[[pd.DataFrame, FunnelContext], StepOutcome]


def decide(units: pd.DataFrame, decision: str, confidence: float | pd.Series,
           reason_codes: str | pd.Series | None = None) -> pd.DataFrame:
    """Stamp a decision on `units`; reason codes are '|'-joined."""
    out = units.copy()
    if reason_codes is not None:
        out["reason_codes"] = reason_codes
    out["decision"] = decision
    out["confidence"] = confidence
    out["explanation"] = [
        f"{codes.replace('|', ', ')}; confidence={conf:.4f}"
        for codes, conf in zip(out["reason_codes"], out["confidence"])
    ]
    return out


def split(units: pd.DataFrame, mask: pd.Series) -> tuple[pd.DataFrame, pd.DataFrame]:
    return units[mask], units[~mask]


def lineage_prefixed(frame: pd.DataFrame, feed: str) -> pd.DataFrame:
    """Rename lineage columns to `<feed>_<column>`; ingest time is dropped so runs are reproducible."""
    frame = frame.drop(columns=[INGESTED_AT], errors="ignore")
    return frame.rename(columns={c: f"{feed}_{c}" for c in LINEAGE_COLUMNS})


def build_units(feeds: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """One reconciliation unit per OMS row."""
    units = lineage_prefixed(feeds["oms"], "oms").reset_index(drop=True)
    units.insert(0, "unit_id", range(1, len(units) + 1))
    return units


def run_funnel(units: pd.DataFrame, steps: list[FunnelStep], ctx: FunnelContext) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (unit results, funnel counts)."""
    decided, counts = [], []
    for step in steps:
        outcome = step.apply(units, ctx)
        decided.append(outcome.decided.assign(deciding_step=step.name))
        counts.append({"step": step.name, "units_in": len(units),
                       "decided": len(outcome.decided), "passed_on": len(outcome.remaining)})
        units = outcome.remaining
    if len(units):
        raise RuntimeError(f"funnel left {len(units)} units undecided")
    results = pd.concat(decided, ignore_index=True).sort_values("unit_id", kind="stable").reset_index(drop=True)
    results["rule_version"] = RULE_VERSION
    hashes = results.reindex(columns=list(HASH_COLUMNS))
    results["input_row_hashes"] = ["|".join(h for h in row if isinstance(h, str))
                                   for row in hashes.itertuples(index=False, name=None)]
    return results, pd.DataFrame(counts, columns=["step", "units_in", "decided", "passed_on"])
