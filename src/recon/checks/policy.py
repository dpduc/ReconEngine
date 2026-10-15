"""Warning checks: OMS order statuses the status policy does not cover, bank payment types without a fee rate."""

from __future__ import annotations

import pandas as pd

from recon.checks import register, warning
from recon.config import PAYMENT, SHIPMENT, RunConfig
from recon.health import SET_STATUS_POLICY, FixOption, Issue
from recon.schema import ORDER_STATUS, PAYMENT_TYPE

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


@register
def payment_type_without_fee_rate(feeds: dict[str, pd.DataFrame], config: RunConfig) -> list[Issue]:
    """With fee rates per payment type, a bank payment type not listed is checked at the fallback `mdr_bps`."""
    bank, policy = feeds.get("bank"), config.resolved_policy
    if not policy.payment_type_fee_bps or bank is None or PAYMENT_TYPE not in bank.columns:
        return []
    kinds = bank[PAYMENT_TYPE].fillna("").astype(str).str.strip()
    issues = []
    for kind in sorted(set(kinds) - set(policy.payment_type_fee_bps) - {""}):
        mask = kinds.eq(kind)
        issues.append(warning(f"payment_type_without_fee_rate:{kind}", "payment_type_without_fee_rate", "bank",
                              bank, mask,
                              f"bank: {int(mask.sum())} credits have payment type '{kind}', which has no fee rate; "
                              f"they are checked at the fallback rate of {policy.mdr_bps} bps"))
    return issues
