"""Final decision for every unit still undecided."""

from __future__ import annotations

import pandas as pd

from recon.funnel.base import FunnelContext, StepOutcome, decide
from recon.funnel.rules import EXCEPTION_CONFIDENCE

CLEAN_CONFIDENCE = 0.9999


def decision(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
    """AUTO_ACCEPT when confidence clears the accept threshold, otherwise REVIEW."""
    confidence = units["clean"].map({True: CLEAN_CONFIDENCE, False: EXCEPTION_CONFIDENCE}).astype(float)
    accept = confidence >= ctx.policy.accept_threshold
    decided = pd.concat([
        decide(units[accept], "AUTO_ACCEPT", confidence[accept]),
        decide(units[~accept], "REVIEW", confidence[~accept]),
    ])
    return StepOutcome(decided=decided, remaining=units.iloc[0:0])
