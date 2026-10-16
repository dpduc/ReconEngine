"""Command line: reconcile a folder from its own config.json.

    python -m recon run test/vn/b_marketplace_heavy --labels test/vn/labels/b_marketplace_heavy --acknowledge "seen"

Health runs first. Blocking issues, or warnings still open without ``--acknowledge``, stop the run
with exit code 1. Otherwise the export bundle and the Health report are written to ``--out``
(``runs/<folder name>`` by default), and decisions, queue and, with labels, accuracy are printed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TextIO

from recon.acknowledgement import acknowledge_warnings
from recon.config import load_config
from recon.health import HealthReport, Severity
from recon.pipeline import inspect, run
from recon.results import ReconRun
from recon.schema import DECISIONS

CONFIG = "config.json"
GATE_CLOSED, NO_CONFIG = 1, 2


def _health_line(report: HealthReport) -> str:
    warnings = report.by_severity(Severity.WARNING)
    acknowledged = sum(report.is_acknowledged(issue) for issue in warnings)
    return (f"Health: {len(report.blocking_issues)} blocking, {len(warnings)} warnings "
            f"({acknowledged} acknowledged)")


def _list_open(report: HealthReport, out: TextIO) -> None:
    for issue in report.open_issues:
        print(f"  {issue.severity.value:<8} {issue.id}: {issue.message}", file=out)


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def summary(outcome: ReconRun) -> list[str]:
    """The lines printed after a completed run."""
    results, scored = outcome.results, outcome.manifest["metrics"]
    counts = results["decision"].value_counts()
    queue = outcome.exception_queue
    lines = [f"Run: {len(results):,} orders",
             *(f"  {decision:<12} {int(counts.get(decision, 0)):>7,}" for decision in DECISIONS),
             f"Queue: {len(queue):,} items, {queue['amount_at_risk'].sum():,.2f} at risk",
             f"Open items: {len(outcome.open_items):,}; orphans: {len(outcome.orphans):,}"]
    if scored.get("accuracy") is not None:
        lines.append(f"Accuracy: {_percent(scored['accuracy'])}; "
                     f"auto-accept precision {_percent(scored['auto_accept_precision'])}")
    if "links" in scored:
        overall = scored["links"]["overall"]
        lines.append(f"Links: precision {_percent(overall['precision'])}, recall {_percent(overall['recall'])}")
    return lines


def run_folder(folder: Path, labels: Path | None, out_dir: Path, note: str | None, out: TextIO = sys.stdout) -> int:
    """Health, then the run of `folder` by its config.json; the exit code."""
    if not (folder / CONFIG).is_file():
        print(f"No {CONFIG} in {folder}. Build one in the app's Load page, or write it by hand.", file=out)
        return NO_CONFIG
    config = load_config(folder / CONFIG)
    report = inspect(folder, config)
    print(_health_line(report), file=out)
    if report.blocking_issues:
        print("Blocked: fix these in the config (see the Health page for options):", file=out)
        _list_open(report, out)
        return GATE_CLOSED
    if not report.gate_open:
        if not note:
            print('Open warnings: review them, then re-run with --acknowledge "<note>" to accept them as they are:',
                  file=out)
            _list_open(report, out)
            return GATE_CLOSED
        config = acknowledge_warnings(config, report, note)
        print(f"Acknowledged {len(report.open_issues)} warnings: {note}", file=out)
    outcome = run(folder, config, labels=labels)
    if outcome.blocked:  # the inputs changed between inspect and run
        _list_open(outcome.health, out)
        return GATE_CLOSED
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {**outcome.bundle(), "health_report.json": outcome.health.to_json()}
    for name, text in files.items():
        (out_dir / name).write_text(text, encoding="utf-8")
    for line in summary(outcome):
        print(line, file=out)
    print(f"Bundle: {out_dir} ({len(files)} files)", file=out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m recon", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    folder_run = commands.add_parser("run", help="reconcile a folder from its config.json")
    folder_run.add_argument("folder", type=Path)
    folder_run.add_argument("--labels", type=Path, help="folder with ground_truth.csv and/or links.csv, to score the run")
    folder_run.add_argument("--out", type=Path, help="where to write the bundle (default: runs/<folder name>)")
    folder_run.add_argument("--acknowledge", metavar="NOTE", help="accept every open Health warning with this note")
    args = parser.parse_args(argv)
    return run_folder(args.folder, args.labels, args.out or Path("runs") / args.folder.name, args.acknowledge)
