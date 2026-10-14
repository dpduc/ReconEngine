"""Info profiles for the Health Report: descriptive EDA that informs config choices.

Add a profile by decorating ``fn(feeds, config) -> JSON-able value | None`` with
``@profile("name")``; ``None`` (e.g. columns missing) leaves it out of the report.
"""

from __future__ import annotations

from typing import Any, Callable

import pandas as pd

from recon.ingest import parse_dates
from recon.config import RunConfig
from recon.schema import CURRENCY, NUMERIC_COLUMNS, ORDER_STATUS

Profile = Callable[[dict[str, pd.DataFrame], RunConfig], Any]

PROFILES: dict[str, Profile] = {}


def profile(name: str) -> Callable[[Profile], Profile]:
    def register(fn: Profile) -> Profile:
        PROFILES[name] = fn
        return fn
    return register


def _percentiles(values: pd.Series, extra: dict[str, float] | None = None) -> dict[str, float]:
    values = values.dropna()
    if values.empty:
        return {"count": 0}
    q = values.quantile([0.5, 0.95, 0.99])
    return {"count": int(len(values)), "min": float(values.min()), **(extra or {}),
            "p50": float(q[0.5]), "p95": float(q[0.95]), "p99": float(q[0.99]), "max": float(values.max())}


@profile("amount_distribution")
def amount_distribution(feeds: dict[str, pd.DataFrame], config: RunConfig) -> dict[str, Any] | None:
    out = {}
    for feed, frame in feeds.items():
        for column in NUMERIC_COLUMNS.get(feed, ()):
            if column in frame.columns:
                values = frame[column]
                out[f"{feed}.{column}"] = _percentiles(values, {"mean": float(values.mean())}
                                                       if values.notna().any() else None)
    return out or None


@profile("order_status_mix")
def order_status_mix(feeds: dict[str, pd.DataFrame], config: RunConfig) -> dict[str, int] | None:
    oms = feeds.get("oms")
    if oms is None or ORDER_STATUS not in oms.columns:
        return None
    counts = oms[ORDER_STATUS].fillna("<missing>").value_counts()
    return {str(status): int(n) for status, n in counts.items()}


@profile("currency_mix")
def currency_mix(feeds: dict[str, pd.DataFrame], config: RunConfig) -> dict[str, dict[str, int]] | None:
    """Rows per currency code in each feed that carries one; feeds may mix currencies."""
    out = {}
    for feed, frame in feeds.items():
        if CURRENCY in frame.columns:
            codes = frame[CURRENCY].astype("string").str.strip().str.upper().fillna("<missing>")
            out[feed] = {str(code): int(n) for code, n in codes.value_counts().items()}
    return out or None


@profile("value_date_lag_days")
def value_date_lag_days(feeds: dict[str, pd.DataFrame], config: RunConfig) -> dict[str, float] | None:
    """Calendar days from transaction to value date: guides the linking date window."""
    bank = feeds.get("bank")
    if bank is None or not {"txn_date", "value_date"} <= set(bank.columns):
        return None
    date_format = config.date_format("bank")
    txn = parse_dates(bank["txn_date"], date_format).dt.normalize()
    value = parse_dates(bank["value_date"], date_format).dt.normalize()
    return _percentiles((value - txn).dt.days)


def run_profiles(feeds: dict[str, pd.DataFrame], config: RunConfig) -> dict[str, Any]:
    out = {}
    for name, fn in PROFILES.items():
        value = fn(feeds, config)
        if value is not None:
            out[name] = value
    return out
