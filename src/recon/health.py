"""Health Report: issues found by the checks and whether the gate is open."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


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

    def by_severity(self, severity: Severity) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity is severity]

    @property
    def blocking_issues(self) -> list[Issue]:
        return self.by_severity(Severity.BLOCKING)

    @property
    def open_issues(self) -> list[Issue]:
        """Issues that keep the gate closed."""
        return self.blocking_issues

    @property
    def gate_open(self) -> bool:
        return not self.open_issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_open": self.gate_open,
            "issues": [issue.to_dict() for issue in self.issues],
            "profiles": self.profiles,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)
