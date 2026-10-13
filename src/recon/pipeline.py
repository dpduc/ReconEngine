"""Public entry point: ``inspect`` then ``run``. UIs and CLIs are thin shells over these."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from recon.checks import run_checks
from recon.config import RunConfig
from recon.evaluation import attach_truth, metrics
from recon.funnel import RULE_VERSION, STEPS, FunnelContext, build_units, run_funnel
from recon.funnel.orphans import orphan_counts, reverse_pass
from recon.health import HealthReport
from recon.ingest import LoadedFeed, file_sha256, load_feed
from recon.resolutions import apply_resolutions
from recon.results import BLOCKED, COMPLETED, ReconRun


@dataclass(frozen=True)
class _Prepared:
    inputs: dict[str, LoadedFeed]
    feeds: dict[str, pd.DataFrame]
    report: HealthReport


def _prepare(sources: str | Path, config: RunConfig) -> _Prepared:
    ingested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    inputs = {
        name: load_feed(name, sources, spec, config.mapping.get(name, {}), ingested_at)
        for name, spec in config.feeds.items()
    }
    feeds = apply_resolutions({name: loaded.frame for name, loaded in inputs.items()}, config)
    report = HealthReport(issues=tuple(run_checks(feeds, config)))
    return _Prepared(inputs, feeds, report)


def inspect(sources: str | Path, config: RunConfig) -> HealthReport:
    """Load, map, normalize and check the feeds declared in `config`.

    `sources` is the folder that feed paths in the config are relative to.
    """
    return _prepare(sources, config).report


def run(sources: str | Path, config: RunConfig) -> ReconRun:
    """Re-inspect; refuse with a blocked run while the gate is closed, else run the funnel."""
    prepared = _prepare(sources, config)
    if not prepared.report.gate_open:
        return ReconRun(status=BLOCKED, health=prepared.report)
    ctx = FunnelContext(feeds=prepared.feeds, policy=config.policy)
    results, funnel_counts = run_funnel(build_units(prepared.feeds), STEPS, ctx)
    orphans = reverse_pass(results, prepared.feeds)
    input_files = {name: {"path": config.feeds[name].path, "sha256": loaded.sha256}
                   for name, loaded in prepared.inputs.items()}
    if config.ground_truth:
        truth_path = Path(sources) / config.ground_truth
        results = attach_truth(results, pd.read_csv(truth_path))
        input_files["ground_truth"] = {"path": config.ground_truth, "sha256": file_sha256(truth_path)}
    manifest = {
        "rule_version": RULE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_files": input_files,
        "run_config": config.to_dict(),
        "metrics": {**metrics(results), "orphans": orphan_counts(orphans)},
    }
    return ReconRun(status=COMPLETED, health=prepared.report, results=results,
                    funnel=funnel_counts, manifest=manifest, feeds=prepared.feeds, orphans=orphans)
