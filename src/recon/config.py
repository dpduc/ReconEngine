"""Run config: the single, serializable source of truth for one run."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from recon.contracts import AMOUNT_TOLERANCE


@dataclass(frozen=True)
class Policy:
    accept_threshold: float = 0.995
    review_threshold: float = 0.80
    max_review_rate: float = 0.01
    mdr_bps: int = 180
    cod_fee_bps: int = 80
    amount_tolerance: float = AMOUNT_TOLERANCE


@dataclass(frozen=True)
class FeedSpec:
    """Where one feed lives. `path` is free-form, relative to the sources folder."""

    path: str
    format: str = "csv"
    encoding: str = "utf-8"
    date_format: str | None = None


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
