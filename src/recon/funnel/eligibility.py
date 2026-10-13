"""Eligibility steps: decide units that can't be reconciled before any evidence is looked at."""

from __future__ import annotations

import pandas as pd

from recon.config import EVIDENCE
from recon.funnel.base import FunnelContext, StepOutcome, split
from recon.funnel.reasons import exception

STATUS_COLUMN = "order_status"


def missing_source_evidence(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
    """No order amount: there is nothing to compare the money against."""
    missing, remaining = split(units, units["order_amount"].isna())
    return StepOutcome(decided=exception(missing, "MISSING_SOURCE_EVIDENCE"), remaining=remaining)


def status_policy(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
    """Statuses the policy doesn't list (unknown or blank too) need a business decision.

    Units passed on carry `requires_payment` / `requires_shipment` for the rule checks.
    Without a status column every unit needs full evidence.
    """
    if STATUS_COLUMN in units:
        statuses = units[STATUS_COLUMN].fillna("").astype(str).str.strip()
        needs = statuses.map(ctx.policy.status_policy)
    else:
        needs = pd.Series([EVIDENCE] * len(units), index=units.index, dtype=object)
    pending, _ = split(units, needs.isna())
    remaining = units[needs.notna()].assign(
        requires_payment=[("payment" in n) for n in needs.dropna()],
        requires_shipment=[("shipment" in n) for n in needs.dropna()],
    )
    return StepOutcome(decided=exception(pending, "ORDER_STATUS_REQUIRES_POLICY"), remaining=remaining)
