"""The reconciliation funnel. ``STEPS`` is the ordered step list; add steps by inserting here."""

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
from recon.funnel.linking import link_exact
from recon.funnel.rules import rule_checks

STEPS: list[FunnelStep] = [
    FunnelStep("link_exact", link_exact),
    FunnelStep("rule_checks", rule_checks),
    FunnelStep("decision", decision),
]

__all__ = [
    "RULE_VERSION", "STEPS", "FunnelContext", "FunnelStep", "StepOutcome",
    "build_units", "decide", "run_funnel", "split",
]
