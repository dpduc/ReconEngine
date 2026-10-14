"""Warning check: OMS order statuses the status policy does not cover."""

from __future__ import annotations

import pandas as pd

from recon.checks import register, warning
from recon.config import PAYMENT, SHIPMENT, RunConfig
from recon.health import SET_STATUS_POLICY, FixOption, Issue
from recon.schema import ORDER_STATUS

# Evidence choices offered for an uncovered status, with their labels.
EVIDENCE_CHOICES = (
    ((PAYMENT, SHIPMENT), "Require payment and shipment"),
    ((PAYMENT,), "Require payment only"),
    ((SHIPMENT,), "Require shipment only"),
    ((), "Require no evidence"),
)
BLANK_STATUS = "(blank)"


@register
def status_not_in_policy(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    """Uncovered statuses end as EXCEPTION(ORDER_STATUS_REQUIRES_POLICY) unless the policy says what they need."""
    oms = feeds.get("oms")
    if oms is None or ORDER_STATUS not in oms.columns:
        return []
    statuses = oms[ORDER_STATUS].fillna("").astype(str).str.strip()
    covered = config.resolved_policy.status_policy
    issues = []
    for status in sorted(set(statuses) - set(covered)):
        mask = statuses.eq(status)
        shown = status or BLANK_STATUS
        fixes = tuple(FixOption(SET_STATUS_POLICY, f"'{shown}': {label.lower()}",
                                {"status": status, "evidence": list(evidence)})
                      for evidence, label in EVIDENCE_CHOICES)
        issues.append(warning(f"status_not_in_policy:{shown}", "status_not_in_policy", "oms", oms, mask,
                              f"oms: {int(mask.sum())} orders have status '{shown}', which the status policy "
                              "does not cover", fixes))
    return issues
