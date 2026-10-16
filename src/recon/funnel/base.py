"""Funnel scaffold: steps share one interface and each unit is decided exactly once.

A step takes the units no earlier step decided and returns ``StepOutcome``:
``decided`` (rows stamped via ``decide``) and ``remaining`` (passed on, possibly
enriched with new columns). The runner records the deciding step and counts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from recon.config import Channel, Period, Policy
from recon.funnel.explain import explain_all
from recon.ingest import parse_dates
from recon.schema import INGESTED_AT, LINEAGE_COLUMNS
from recon.timing import StageTimer

RULE_VERSION = "2.1.0"  # bump whenever funnel logic changes

HASH_COLUMNS = ("oms_row_hash", "bank_row_hash", "logistics_row_hash", "settlement_row_hash")


@dataclass(frozen=True)
class FunnelContext:
    feeds: dict[str, pd.DataFrame]  # canonical feeds after resolutions
    policy: Policy  # with the run config's resolutions applied
    date_formats: dict[str, str | None] = field(default_factory=dict)  # feed -> declared strftime format
    fx_rates: pd.DataFrame | None = None  # the policy's rate table, read (see funnel.fx)
    order_ref_patterns: tuple[str, ...] = ()  # the chains' own order-reference patterns (see linking)
    marketplaces: dict[str, Channel] = field(default_factory=dict)  # OMS channel value -> marketplace channel
    cod_channels: tuple[str, ...] = ()  # OMS channel values whose COD orders are paid by carrier remittances
    channels: dict[str, Channel] = field(default_factory=dict)  # every declared channel, for its payment terms
    period: Period | None = None  # with a cut-off, money not yet due is OPEN (see funnel.periods)
    # Settlement batch results: payouts (funnel.settlement) and remittances (funnel.remittance).
    batches: pd.DataFrame | None = None
    payout_credit_rows: frozenset[int] = frozenset()  # bank source rows naming a batch: not free to link

    def dates(self, feed: str, values: pd.Series, utc: bool = False) -> pd.Series:
        """`values` (a date column of `feed`) parsed as the feed declares."""
        return parse_dates(values, self.date_formats.get(feed), utc=utc)


@dataclass(frozen=True)
class StepOutcome:
    decided: pd.DataFrame
    remaining: pd.DataFrame


def pass_on(units: pd.DataFrame) -> StepOutcome:
    """Decide nothing; hand every unit (possibly enriched) to the next step."""
    return StepOutcome(decided=units.iloc[0:0], remaining=units)


def cod_remittance(frame: pd.DataFrame) -> pd.Series:
    """What the 3PL remits for a COD order: cash collected less shipping and COD fees (missing = 0)."""
    return frame["cod_collected"].fillna(0) - frame["ship_fee"].fillna(0) - frame["cod_fee"].fillna(0)


@dataclass(frozen=True)
class FunnelStep:
    name: str
    apply: Callable[[pd.DataFrame, FunnelContext], StepOutcome]


def decide(units: pd.DataFrame, decision: str, confidence: float | pd.Series,
           reason_codes: str | pd.Series | None = None) -> pd.DataFrame:
    """Stamp a decision on `units`; reason codes are '|'-joined and explained in plain language."""
    out = units.copy()
    if reason_codes is not None:
        out["reason_codes"] = reason_codes
    out["decision"] = decision
    out["confidence"] = confidence
    out["explanation"] = explain_all(out)
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


def run_funnel(units: pd.DataFrame, steps: list[FunnelStep], ctx: FunnelContext,
               timer: StageTimer | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (unit results, funnel counts); `timer` records each step as 'funnel:<step>'."""
    timer = timer or StageTimer()
    decided, counts = [], []
    for step in steps:
        with timer.stage(f"funnel:{step.name}"):
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
