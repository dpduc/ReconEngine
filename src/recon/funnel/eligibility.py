"""Eligibility steps: decide units that can't be reconciled before any evidence is looked at.

Decided units still carry the records that name them exactly, for lineage; those records
are theirs, not orphans.
"""

from __future__ import annotations

import pandas as pd

from recon.config import EVIDENCE, PAYMENT, SHIPMENT
from recon.funnel.base import FunnelContext, StepOutcome, split
from recon.funnel.linking import attach_exact
from recon.funnel.reasons import exception
from recon.schema import ORDER_STATUS


def missing_source_evidence(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
    """No order amount: there is nothing to compare the money against."""
    missing, remaining = split(units, units["order_amount"].isna())
    return StepOutcome(decided=exception(attach_exact(missing, ctx), "MISSING_SOURCE_EVIDENCE"),
                       remaining=remaining)


def status_policy(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
    """Statuses the policy doesn't list (unknown or blank too) need a business decision.

    Units passed on carry `requires_payment` / `requires_shipment` for the rule checks.
    Without a status column every unit needs full evidence.
    """
    if ORDER_STATUS in units:
        statuses = units[ORDER_STATUS].fillna("").astype(str).str.strip()
        required = statuses.map(ctx.policy.status_policy)
    else:
        required = pd.Series([EVIDENCE] * len(units), index=units.index, dtype=object)
    pending, _ = split(units, required.isna())
    covered = required.dropna()
    remaining = units[required.notna()].assign(
        requires_payment=[PAYMENT in evidence for evidence in covered],
        requires_shipment=[SHIPMENT in evidence for evidence in covered],
    )
    return StepOutcome(decided=exception(attach_exact(pending, ctx), "ORDER_STATUS_REQUIRES_POLICY"),
                       remaining=remaining)
