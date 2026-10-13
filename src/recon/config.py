"""Run config: the single, serializable source of truth for one run."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from recon.health import SET_STATUS_POLICY

# Evidence an order status can require before its unit ties out.
PAYMENT, SHIPMENT = "payment", "shipment"
EVIDENCE = (PAYMENT, SHIPMENT)


def _default_status_policy() -> dict[str, tuple[str, ...]]:
    return {"delivered": EVIDENCE, "shipped": EVIDENCE}


@dataclass(frozen=True)
class Policy:
    accept_threshold: float = 0.995
    mdr_bps: int = 180
    cod_fee_bps: int = 80
    amount_tolerance: float = 0.01  # absolute, in the feed currency
    link_date_window_days: int = 4  # linking pass 2: |value_date - order date| allowed
    # order status -> the EVIDENCE it requires; unlisted statuses are pending policy
    status_policy: dict[str, tuple[str, ...]] = field(default_factory=_default_status_policy)

    def __post_init__(self) -> None:
        unknown = {e for required in self.status_policy.values() for e in required} - set(EVIDENCE)
        if unknown:
            raise ValueError(f"unknown evidence in status_policy: {sorted(unknown)}")
        normalized = {str(status): tuple(required) for status, required in self.status_policy.items()}
        object.__setattr__(self, "status_policy", normalized)

    def __hash__(self) -> int:
        return hash(tuple((f, repr(getattr(self, f))) for f in self.__dataclass_fields__))


@dataclass(frozen=True)
class FeedSpec:
    """Where one feed lives. `path` is free-form, relative to the sources folder."""

    path: str
    format: str = "csv"
    encoding: str = "utf-8"
    date_format: str | None = None
    delimiter: str | None = None  # CSV only; None = detect from the header line


@dataclass(frozen=True)
class Resolution:
    """An analyst's decision on one Health Report issue."""

    issue_id: str
    option: str
    params: dict[str, Any] = field(default_factory=dict)
    note: str = ""


@dataclass(frozen=True)
class RunConfig:
    feeds: dict[str, FeedSpec]
    mapping: dict[str, dict[str, str]] = field(default_factory=dict)  # feed -> {canonical: source column}
    policy: Policy = Policy()
    resolutions: tuple[Resolution, ...] = ()
    ground_truth: str | None = None  # optional truth CSV, relative to the sources folder

    @property
    def resolved_policy(self) -> Policy:
        """`policy` with the status evidence that `set_status_policy` resolutions chose."""
        chosen = {r.params["status"]: tuple(r.params["evidence"])
                  for r in self.resolutions if r.option == SET_STATUS_POLICY}
        if not chosen:
            return self.policy
        return replace(self.policy, status_policy={**self.policy.status_policy, **chosen})

    def date_format(self, feed: str) -> str | None:
        """The feed's declared strftime date format, if any."""
        spec = self.feeds.get(feed)
        return spec.date_format if spec else None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["resolutions"] = [asdict(r) for r in self.resolutions]
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunConfig:
        return cls(
            feeds={name: FeedSpec(**spec) for name, spec in data["feeds"].items()},
            mapping={feed: dict(cols) for feed, cols in data.get("mapping", {}).items()},
            policy=Policy(**data.get("policy", {})),
            resolutions=tuple(Resolution(**r) for r in data.get("resolutions", [])),
            ground_truth=data.get("ground_truth"),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


def load_config(path: str | Path) -> RunConfig:
    """Read a run config from JSON or (with PyYAML installed) YAML."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yaml", ".yml"}:
        import yaml

        return RunConfig.from_dict(yaml.safe_load(text))
    return RunConfig.from_dict(json.loads(text))


def save_config(config: RunConfig, path: str | Path) -> None:
    path = Path(path)
    if path.suffix in {".yaml", ".yml"}:
        import yaml

        path.write_text(yaml.safe_dump(config.to_dict(), sort_keys=True), encoding="utf-8")
    else:
        path.write_text(config.to_json(), encoding="utf-8")
