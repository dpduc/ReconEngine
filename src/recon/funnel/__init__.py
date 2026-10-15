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
from recon.funnel.linking import link_candidates, link_constrained, link_exact
from recon.funnel.routing import route_review
from recon.funnel.rules import RULE_STEPS

STEPS: list[FunnelStep] = [
    FunnelStep("missing_source_evidence", missing_source_evidence),
    FunnelStep("status_policy", status_policy),
    FunnelStep("link_exact", link_exact),
    FunnelStep("link_constrained", link_constrained),
    FunnelStep("link_candidates", link_candidates),
    FunnelStep("route_review", route_review),
    *RULE_STEPS,
    FunnelStep("decision", decision),
]

# What each step is for, so time can be reported per group (matching = linking + rule checks).
LINKING, RULE_CHECKS, DECISION, POLICY_ROUTING = "linking", "rule checks", "decision", "policy/routing"
STEP_GROUPS: dict[str, str] = {
    "missing_source_evidence": POLICY_ROUTING,
    "status_policy": POLICY_ROUTING,
    "link_exact": LINKING,
    "link_constrained": LINKING,
    "link_candidates": LINKING,
    "route_review": POLICY_ROUTING,
    **{step.name: RULE_CHECKS for step in RULE_STEPS},
    "decision": DECISION,
}
MATCHING_GROUPS = (LINKING, RULE_CHECKS)

__all__ = [
    "DECISION", "LINKING", "MATCHING_GROUPS", "POLICY_ROUTING", "RULE_CHECKS", "STEP_GROUPS",
    "RULE_VERSION", "STEPS", "FunnelContext", "FunnelStep", "StepOutcome",
    "build_units", "decide", "run_funnel", "split",
]
