"""Plain-language guidance for Health issues: what was found, what happens next, what to pick.

``guide(issue)`` looks up the guide for the issue's check. Each guide recommends one of the
issue's own fix options (by index) and says why, so an analyst without reconciliation
experience can decide. Add a guide whenever a new check is registered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from recon.health import (
    ACKNOWLEDGE,
    DUPLICATE_STRATEGY,
    EXCLUDE_ROWS,
    REMAP,
    ROUTE_TO_REVIEW,
    SET_DATE_FORMAT,
    SET_STATUS_POLICY,
    Issue,
    Severity,
)
from recon.config import PAYMENT, SHIPMENT

LARGE_SHARE = 0.2  # an issue touching more of a feed than this probably means a wrong file, period or mapping


@dataclass(frozen=True)
class Guidance:
    meaning: str  # what was found, in plain language
    next_step: str  # what happens to the affected records
    needs_decision: bool  # True: a real choice; False: sign-off only (acknowledge)
    recommended: int | None  # index into issue.fix_options
    why: str  # why the recommended option
    caution: str | None = None
    generic: bool = False  # True when no guide exists for the check

    def to_dict(self, issue: Issue) -> dict[str, Any]:
        option = issue.fix_options[self.recommended] if self.recommended is not None else None
        return {"meaning": self.meaning, "next_step": self.next_step, "needs_decision": self.needs_decision,
                "recommended_option": option.label if option else None, "why": self.why, "caution": self.caution}


def _option(issue: Issue, option_id: str, **params: Any) -> int | None:
    """Index of the first fix option with `option_id` whose params include `params`."""
    for index, option in enumerate(issue.fix_options):
        if option.id == option_id and all(option.params.get(k) == v for k, v in params.items()):
            return index
    return None


def _part(issue: Issue, index: int) -> str:
    """A segment of the issue id, e.g. the column in 'missing_amount:oms:order_amount'."""
    parts = issue.id.split(":")
    return parts[index] if len(parts) > abs(index) else ""


def _rows(issue: Issue) -> str:
    return f"{issue.affected_rows:,} row(s)"


SIGN_OFF_WHY = "Nothing to fix in the data; acknowledging records that you saw it before the run."


def _missing_column(issue: Issue) -> Guidance:
    column = _part(issue, -1)
    return Guidance(
        meaning=f"The engine could not tell which column of the {issue.feed} file holds '{column}', "
                "which it needs to reconcile.",
        next_step="Nothing can run until the column is mapped.",
        needs_decision=True, recommended=_option(issue, REMAP),
        why=f"Pick the source column that holds {column}. If no column fits, the wrong file was probably loaded.")


def _unparseable_value(issue: Issue) -> Guidance:
    column = _part(issue, -1)
    return Guidance(
        meaning=f"{_rows(issue)} in the {issue.feed} file have a '{column}' that is not a number "
                "(e.g. text, or a comma used as the decimal separator).",
        next_step="These rows block the run until they are excluded or the file is corrected.",
        needs_decision=True, recommended=_option(issue, EXCLUDE_ROWS),
        why="If only a few rows are broken, exclude them with a reason and get them fixed at the source. "
            "If most rows are, the export settings are wrong: re-export the file instead.")


def _unparseable_date(issue: Issue) -> Guidance:
    column = _part(issue, -1)
    date_format = _option(issue, SET_DATE_FORMAT)
    return Guidance(
        meaning=f"{_rows(issue)} in the {issue.feed} file have a '{column}' date the engine cannot read "
                "(it expects e.g. 2024-01-31).",
        next_step="These rows block the run until the date format is set or the rows are excluded.",
        needs_decision=True, recommended=date_format if date_format is not None else _option(issue, EXCLUDE_ROWS),
        why="If the dates look like 31/01/2024, set the matching date format: nothing is lost. "
            "Exclude rows only when their dates are truly broken.")


def _duplicate_key(issue: Issue) -> Guidance:
    key = _part(issue, -1)
    return Guidance(
        meaning=f"The same {key} appears more than once in the {issue.feed} file, "
                "so the engine cannot tell which row is the real one.",
        next_step="The run is blocked until you choose how to treat the duplicates.",
        needs_decision=True, recommended=_option(issue, DUPLICATE_STRATEGY, strategy="review"),
        why="Routing to review keeps every row and lets someone pick the right one. Choose 'keep the latest' "
            "only if you know the file holds updated versions of the same record.")


def _missing_amount(issue: Issue) -> Guidance:
    column = _part(issue, -1)
    order_amount = issue.feed == "oms" and column == "order_amount"
    return Guidance(
        meaning=f"{_rows(issue)} in the {issue.feed} file have no '{column}'.",
        next_step=("These orders become MISSING_SOURCE_EVIDENCE exceptions: there is no amount to check "
                   "the money against." if order_amount else
                   "Missing values count as 0 in the comparisons, so affected orders may show up as mismatches."),
        needs_decision=False, recommended=_option(issue, ACKNOWLEDGE),
        why=SIGN_OFF_WHY + " Ask the source system's owner if the count looks high.")


def _zero_amount(issue: Issue) -> Guidance:
    return Guidance(
        meaning=f"{_rows(issue)} in the {issue.feed} file have an amount of exactly 0.",
        next_step="They are compared like any other row; a 0 credit against a real order shows up as "
                  "AMOUNT_MISMATCH.",
        needs_decision=False, recommended=_option(issue, ACKNOWLEDGE),
        why=SIGN_OFF_WHY + " Zero amounts are often free orders, test transactions or reversals.")


def _amount_outlier(issue: Issue) -> Guidance:
    return Guidance(
        meaning=f"{_rows(issue)} in the {issue.feed} file have a '{_part(issue, -1)}' far larger or smaller "
                "than the rest of the file.",
        next_step="They are reconciled normally; nothing is changed.",
        needs_decision=False, recommended=_option(issue, ACKNOWLEDGE),
        why=SIGN_OFF_WHY + " Glance at the sample rows: big real orders are fine, but values 1,000x the usual "
            "suggest a decimal or unit error at the source.")


COVERAGE = {
    "no_payment": ("orders have no bank credit naming them.",
                   "They become MISSING_PAYMENT exceptions (unless their status needs no payment)."),
    "no_shipment": ("orders have no shipment record.",
                    "They become MISSING_SHIPMENT exceptions (unless their status needs no shipment)."),
    "no_order": ("records name no order in the OMS file.",
                 "They are reported as orphans: ORPHAN_PAYMENT for bank credits, ORPHAN_SHIPMENT for shipments."),
}


def _coverage(issue: Issue) -> Guidance:
    found, next_step = COVERAGE.get(_part(issue, -1), ("records have no counterpart in another file.",
                                                        "They are flagged as exceptions in the results."))
    return Guidance(
        meaning=f"{issue.affected_rows:,} {issue.feed} {found}",
        next_step=next_step,
        needs_decision=False, recommended=_option(issue, ACKNOWLEDGE),
        why="Finding these gaps is what reconciliation is for: acknowledge now and work the exceptions in "
            "Review. A very large count usually means the files cover different periods.")


def _picked_after_delivered(issue: Issue) -> Guidance:
    return Guidance(
        meaning=f"{_rows(issue)} are shipments picked up after they were delivered, which is impossible.",
        next_step="They become TIMING_MISMATCH exceptions (low priority).",
        needs_decision=False, recommended=_option(issue, ACKNOWLEDGE),
        why=SIGN_OFF_WHY + " This is usually a data-entry or time-zone problem at the 3PL.")


def _placeholder_timestamp(issue: Issue) -> Guidance:
    return Guidance(
        meaning=f"Every '{_part(issue, 2)}' value here has the time 00:00:00, so these are probably estimated "
                "dates rather than real event times.",
        next_step="Matching is not affected; just don't trust durations computed from these dates.",
        needs_decision=False, recommended=_option(issue, ACKNOWLEDGE), why=SIGN_OFF_WHY)


# Status words -> the evidence such an order can be expected to have, and why.
STATUS_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
    (("cancel", "void", "unavailable", "refund", "reject", "fail", "return"), (),
     "Canceled or unavailable orders normally have no payment or shipment to check. If paid orders are "
     "refunded later, choose 'payment only' instead."),
    (("invoic", "process", "approv", "confirm", "paid"), (PAYMENT,),
     "These orders are paid or being prepared but not shipped yet, so only a payment can be expected."),
    (("ship", "deliver", "complet", "fulfil", "dispatch"), (PAYMENT, SHIPMENT),
     "Shipped or delivered orders should have both a payment and a shipment."),
)


def _status_not_in_policy(issue: Issue) -> Guidance:
    status = next((o.params["status"] for o in issue.fix_options if o.id == SET_STATUS_POLICY), "")
    shown = status or "(blank)"
    meaning = (f"{issue.affected_rows:,} orders have status '{shown}', and the status policy does not say what "
               "proof (payment, shipment) such an order should have.")
    next_step = ("If you leave it pending, these orders become ORDER_STATUS_REQUIRES_POLICY exceptions for "
                 "finance to decide. If you set a rule, they are reconciled against it.")
    lowered = status.lower()
    for words, evidence, why in STATUS_RULES:
        if any(word in lowered for word in words):
            index = _option(issue, SET_STATUS_POLICY, evidence=list(evidence))
            if index is not None:
                return Guidance(meaning, next_step, True, index, why)
    return Guidance(meaning, next_step, True, _option(issue, ACKNOWLEDGE),
                    "The engine does not recognise this status: leave it pending and ask finance what it means.")


def _currency_mismatch(issue: Issue) -> Guidance:
    return Guidance(
        meaning=f"{issue.affected_rows:,} bank credit(s) are in a different currency than the order they belong to.",
        next_step="Until you decide, they are CURRENCY_MISMATCH exceptions: the amounts are not compared.",
        needs_decision=True, recommended=_option(issue, ROUTE_TO_REVIEW),
        why="Routing to review is the safe default. Correct the code only if you know the bank labels the "
            "currency wrongly; convert if treasury gives you exchange rates.")


GUIDES: dict[str, Callable[[Issue], Guidance]] = {
    "missing_required_column": _missing_column,
    "unparseable_value": _unparseable_value,
    "unparseable_date": _unparseable_date,
    "duplicate_key": _duplicate_key,
    "missing_amount": _missing_amount,
    "zero_amount": _zero_amount,
    "amount_outlier": _amount_outlier,
    "coverage": _coverage,
    "picked_after_delivered": _picked_after_delivered,
    "placeholder_timestamp": _placeholder_timestamp,
    "status_not_in_policy": _status_not_in_policy,
    "currency_mismatch": _currency_mismatch,
}


def _caution(issue: Issue, advice: Guidance) -> str | None:
    if not issue.total_rows or issue.check == "missing_required_column":
        return None
    share = issue.affected_rows / issue.total_rows
    if share <= LARGE_SHARE:
        return None
    return (f"This affects {share:.0%} of the {issue.feed} rows. That many usually means a wrong file, period or "
            f"column mapping: check that before {'deciding' if advice.needs_decision else 'acknowledging'}.")


def guide(issue: Issue) -> Guidance:
    """Guidance for `issue`: specific to its check, or a generic fallback for unknown checks."""
    make = GUIDES.get(issue.check)
    if make is None:
        decide = issue.severity is Severity.BLOCKING
        advice = Guidance(issue.message, "See the issue message.", decide,
                          None if decide else _option(issue, ACKNOWLEDGE), "No specific guidance for this check.",
                          generic=True)
    else:
        advice = make(issue)
    caution = _caution(issue, advice)
    return advice if caution is None else Guidance(**{**advice.__dict__, "caution": caution})
