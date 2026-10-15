"""Public entry point: ``inspect`` then ``run``. UIs and CLIs are thin shells over these.

Everything a shell needs (config types, fix-option ids, schema names) is re-exported here.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from recon.acknowledgement import acknowledge_warnings, acknowledgements
from recon.checks import run_checks, run_raw_checks
from recon.config import (
    EVIDENCE,
    PAYMENT,
    SHIPMENT,
    FeedSpec,
    FxPolicy,
    Policy,
    Resolution,
    RunConfig,
    load_config,
    save_config,
)
from recon.evaluation import attach_truth, link_metrics, metrics
from recon.funnel import RULE_VERSION, STEPS, FunnelContext, build_units, run_funnel
from recon.funnel.fx import read_rate_table
from recon.guidance import Guidance, guide
from recon.funnel.orphans import orphan_counts, reverse_pass
from recon.health import (
    ACKNOWLEDGE,
    CONVERT_CURRENCY,
    CORRECT_CURRENCY,
    DUPLICATE_STRATEGY,
    EXCLUDE_ROWS,
    REMAP,
    ROUTE_TO_REVIEW,
    SET_DATE_FORMAT,
    SET_STATUS_POLICY,
    FixOption,
    HealthReport,
    Issue,
    Severity,
)
from recon.ingest import LoadedFeed, Sources, describe_sources, load_feed, parse_numeric, read_source, sha256
from recon.mapping import (
    DraftConfig,
    MappingProfile,
    Suggestion,
    draft_config,
    load_profile,
    save_profile,
    suggest_mapping,
)
from recon.profiles import run_profiles
from recon.resolutions import apply_resolutions, resolve, undo
from recon.results import BLOCKED, COMPLETED, EXCLUDED_COLUMNS, ReconRun, watchlist_summary
from recon.schema import (
    AMOUNT_COLUMNS,
    AUTO_ACCEPT,
    EXCEPTION,
    EXCLUDED_BY,
    EXCLUSION_REASON,
    FEED_LABELS,
    FEEDS,
    FILE_EXTENSIONS,
    REVIEW,
    ROW_HASH,
    SOURCE_FILE,
    SOURCE_ROW,
)
from recon.timing import RunMeter, StageTimer

__all__ = [
    "inspect", "run", "Sources", "describe_sources",
    # config
    "RunConfig", "FeedSpec", "Policy", "FxPolicy", "Resolution", "load_config", "save_config",
    "EVIDENCE", "PAYMENT", "SHIPMENT",
    # health and fixes
    "HealthReport", "Issue", "FixOption", "Severity", "Guidance", "guide", "resolve", "undo", "acknowledge_warnings",
    "ACKNOWLEDGE", "REMAP", "EXCLUDE_ROWS", "DUPLICATE_STRATEGY", "SET_DATE_FORMAT", "SET_STATUS_POLICY",
    "CORRECT_CURRENCY", "ROUTE_TO_REVIEW", "CONVERT_CURRENCY",
    # mapping
    "DraftConfig", "MappingProfile", "Suggestion", "draft_config", "suggest_mapping", "load_profile", "save_profile",
    # results and schema
    "ReconRun", "AUTO_ACCEPT", "REVIEW", "EXCEPTION", "FEEDS", "FEED_LABELS", "FILE_EXTENSIONS", "AMOUNT_COLUMNS",
    "ROW_HASH", "SOURCE_ROW",
]


@dataclass(frozen=True)
class _Prepared:
    inputs: dict[str, LoadedFeed]
    feeds: dict[str, pd.DataFrame]
    report: HealthReport
    excluded: pd.DataFrame


def _split_excluded(feeds: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Take rows that resolutions marked as excluded out of the feeds, keeping them as a report."""
    kept, excluded = {}, []
    for name, frame in feeds.items():
        if EXCLUDED_BY not in frame:
            kept[name] = frame
            continue
        mask = frame[EXCLUDED_BY].notna()
        rows = frame.loc[mask, [SOURCE_FILE, SOURCE_ROW, ROW_HASH, EXCLUDED_BY, EXCLUSION_REASON]]
        excluded.append(rows.rename(columns={EXCLUDED_BY: "issue_id", EXCLUSION_REASON: "reason"})
                        .assign(feed=name))
        kept[name] = frame.loc[~mask].drop(columns=[EXCLUDED_BY, EXCLUSION_REASON])
    table = pd.concat(excluded, ignore_index=True) if excluded else pd.DataFrame()
    return kept, table.reindex(columns=list(EXCLUDED_COLUMNS))


def _prepare(sources: Sources, config: RunConfig, timer: StageTimer | None = None) -> _Prepared:
    stage = (timer or StageTimer()).stage
    ingested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with stage("load"):
        inputs = {
            name: load_feed(name, sources, spec, config.mapping.get(name, {}), ingested_at)
            for name, spec in config.feeds.items()
        }
    with stage("resolutions"):
        feeds = apply_resolutions({name: loaded.frame for name, loaded in inputs.items()}, config)
        feeds, excluded = _split_excluded(feeds)
    with stage("raw_checks"):
        raw_issues = run_raw_checks(feeds, config)
    with stage("parse_numeric"):
        feeds = {name: parse_numeric(name, frame) for name, frame in feeds.items()}
    with stage("checks"):
        issues = run_checks(feeds, config)
    with stage("profiles"):
        profiles = run_profiles(feeds, config)
    report = HealthReport(issues=tuple(raw_issues + issues), profiles=profiles, acknowledged=acknowledgements(config))
    return _Prepared(inputs, feeds, report, excluded)


def inspect(sources: Sources, config: RunConfig) -> HealthReport:
    """Load, map, normalize and check the feeds declared in `config`.

    `sources` is the folder that feed paths in the config are relative to, or the files
    themselves held in memory as `{relative path: bytes}`.
    """
    return _prepare(sources, config).report


def run(sources: Sources, config: RunConfig) -> ReconRun:
    """Re-inspect; refuse with a blocked run while the gate is closed, else run the funnel."""
    with RunMeter() as meter:
        return _run(sources, config, meter)


def _run(sources: Sources, config: RunConfig, meter: RunMeter) -> ReconRun:
    timer = meter.timer
    prepared = _prepare(sources, config, timer)
    input_bytes = int(sum(loaded.size_bytes for loaded in prepared.inputs.values()))
    if not prepared.report.gate_open:  # the stages that ran (load through profiles) are still recorded
        manifest = {"performance": meter.performance(units=0, input_bytes=input_bytes)}
        return ReconRun(status=BLOCKED, health=prepared.report, excluded=prepared.excluded, manifest=manifest)
    policy = config.resolved_policy
    rate_table = read_source(sources, policy.fx.rate_table) if policy.fx and policy.fx.rate_table else None
    ctx = FunnelContext(feeds=prepared.feeds, policy=policy,
                        date_formats={feed: config.date_format(feed) for feed in config.feeds},
                        fx_rates=read_rate_table(rate_table) if rate_table is not None else None)
    with timer.stage("build_units"):
        units = build_units(prepared.feeds)
    results, funnel_counts = run_funnel(units, STEPS, ctx, timer)
    with timer.stage("reverse_pass"):
        orphans, reverse_counts = reverse_pass(results, prepared.feeds)
    funnel_counts = pd.concat([funnel_counts, pd.DataFrame([reverse_counts])], ignore_index=True)
    input_files = {name: {"path": config.feeds[name].path, "sha256": loaded.sha256}
                   for name, loaded in prepared.inputs.items()}
    if rate_table is not None:
        input_files["fx_rates"] = {"path": policy.fx.rate_table, "sha256": sha256(rate_table)}
    if config.ground_truth:
        with timer.stage("ground_truth"):
            truth = read_source(sources, config.ground_truth)
            results = attach_truth(results, pd.read_csv(io.BytesIO(truth)))
        input_files["ground_truth"] = {"path": config.ground_truth, "sha256": sha256(truth)}
    links = None
    if config.link_truth:
        with timer.stage("link_truth"):
            link_truth = read_source(sources, config.link_truth)
            links = link_metrics(results, pd.read_csv(io.BytesIO(link_truth), dtype=str, keep_default_na=False))
        input_files["link_truth"] = {"path": config.link_truth, "sha256": sha256(link_truth)}
    manifest = {
        "rule_version": RULE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_files": input_files,
        "run_config": config.to_dict(),
        "metrics": {**metrics(results), "orphans": orphan_counts(orphans),
                    "close_watchlist": watchlist_summary(results), **({"links": links} if links else {})},
        "excluded_rows": {k: int(v) for k, v in prepared.excluded["feed"].value_counts().items()},
    }
    manifest["performance"] = meter.performance(units=int(len(results)), input_bytes=input_bytes)
    return ReconRun(status=COMPLETED, health=prepared.report, results=results,
                    funnel=funnel_counts, manifest=manifest, feeds=prepared.feeds, orphans=orphans,
                    excluded=prepared.excluded)
