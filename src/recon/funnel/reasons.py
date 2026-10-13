"""Exception reason codes and their triage priority."""

from __future__ import annotations

import pandas as pd

from recon.funnel.base import decide
from recon.schema import EXCEPTION

EXCEPTION_CONFIDENCE = 0.15

# Higher = handle first. Missing money and wrong money outrank evidence and timing gaps.
PRIORITY = {
    "MISSING_PAYMENT": 3,
    "AMOUNT_MISMATCH": 3,
    "MISSING_SHIPMENT": 2,
    "FEE_MISMATCH": 2,
    "COD_FEE_MISMATCH": 2,
    "MISSING_SOURCE_EVIDENCE": 2,
    "ORDER_STATUS_REQUIRES_POLICY": 1,
    "TIMING_MISMATCH": 1,
}


def exception(units: pd.DataFrame, reason: str) -> pd.DataFrame:
    """Decide `units` as EXCEPTION(reason, priority)."""
    return decide(units, EXCEPTION, EXCEPTION_CONFIDENCE, reason).assign(priority=PRIORITY[reason])
