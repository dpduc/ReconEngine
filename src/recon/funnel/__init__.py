"""The reconciliation funnel. ``STEPS`` is the ordered step list; add steps by inserting here.

Order: eligibility (source evidence, status policy) -> linking -> rule checks -> decision.
"""

from recon.funnel.base import (
    RULE_VERSION,
    FunnelContext,
    FunnelStep,
    StepOutcome,
    build_units,
    decide,
    run_funnel,
    split,
)
from recon.funnel.decision import decision
from recon.funnel.eligibility import missing_source_evidence, status_policy
from recon.funnel.linking import link_exact
from recon.funnel.rules import RULE_STEPS

STEPS: list[FunnelStep] = [
    FunnelStep("missing_source_evidence", missing_source_evidence),
    FunnelStep("status_policy", status_policy),
    FunnelStep("link_exact", link_exact),
    *RULE_STEPS,
    FunnelStep("decision", decision),
]

__all__ = [
    "RULE_VERSION", "STEPS", "FunnelContext", "FunnelStep", "StepOutcome",
    "build_units", "decide", "run_funnel", "split",
]
