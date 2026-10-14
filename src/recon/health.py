"""Health Report: issues found by the checks and whether the gate is open."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

# Fix option ids; a resolution names the one the analyst chose.
ACKNOWLEDGE = "acknowledge"  # accept a warning as-is; needs a non-empty note
REMAP = "remap"  # params: feed, canonical, source
EXCLUDE_ROWS = "exclude_rows"  # params: feed, source_rows, reason
DUPLICATE_STRATEGY = "duplicate_strategy"  # params: feed, strategy
SET_DATE_FORMAT = "set_date_format"  # params: feed, date_format
SET_STATUS_POLICY = "set_status_policy"  # params: status, evidence
CORRECT_CURRENCY = "correct_currency"  # params: feed, source_rows, currency
ROUTE_TO_REVIEW = "route_to_review"  # params: feed, source_rows, reason (a reason code)
CONVERT_CURRENCY = "convert_currency"  # params: rate_column, rate_table, tolerance (see FxPolicy)


class Severity(str, Enum):
    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class FixOption:
    id: str
    label: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Issue:
    id: str  # stable across re-inspections, e.g. "missing_column:oms:order_amount"
    check: str
    severity: Severity
    feed: str
    affected_rows: int
    message: str
    sample_rows: tuple[dict[str, Any], ...] = ()
    fix_options: tuple[FixOption, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["severity"] = self.severity.value
        return out


@dataclass(frozen=True)
class HealthReport:
    issues: tuple[Issue, ...] = ()
    profiles: dict[str, Any] = field(default_factory=dict)
    acknowledged: dict[str, str] = field(default_factory=dict)  # warning id -> acknowledgement note

    def by_severity(self, severity: Severity) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity is severity]

    @property
    def blocking_issues(self) -> list[Issue]:
        return self.by_severity(Severity.BLOCKING)

    def is_acknowledged(self, issue: Issue) -> bool:
        return issue.severity is Severity.WARNING and issue.id in self.acknowledged

    @property
    def open_issues(self) -> list[Issue]:
        """Issues that keep the gate closed: blocking ones and unacknowledged warnings."""
        return [issue for issue in self.issues
                if issue.severity is Severity.BLOCKING
                or (issue.severity is Severity.WARNING and not self.is_acknowledged(issue))]

    @property
    def gate_open(self) -> bool:
        return not self.open_issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_open": self.gate_open,
            "issues": [{**issue.to_dict(), "acknowledged": self.is_acknowledged(issue),
                        "acknowledgement_note": self.acknowledged.get(issue.id) if self.is_acknowledged(issue) else None}
                       for issue in self.issues],
            "profiles": self.profiles,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    def to_html(self, title: str = "Health Report") -> str:
        """A standalone HTML page (inline styles, no scripts) to share with data owners."""
        from recon.report_html import render_health_report

        return render_health_report(self, title)
