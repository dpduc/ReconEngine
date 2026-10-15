"""Runtime and cost harness: scale the benchmark, time reconciliations, report percentiles and cost.

    python -m recon.perf report --sizes 100000 1000000 --repeats 10 3 --out reports/performance.md

Each measurement runs in its own process, so peak memory is that run's alone. Timings come
from the stage timer every run records in its manifest (``manifest["performance"]``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from recon.timing import process_peak_rss_bytes

FILES = ("oms_orders.csv", "bank_statement.csv", "logistics_report.csv", "ground_truth.csv")
MATCHING_PREFIX = "funnel:"  # linking, rule checks and the decision


# --- scaling ---------------------------------------------------------------------------------

def _fresh_id(order_id: str, copy: int) -> str:
    return order_id if copy == 0 else hashlib.md5(f"{order_id}:{copy}".encode()).hexdigest()


def scale_benchmark(source_dir: str | Path, out_dir: str | Path, units: int) -> Path:
    """Copy the generated benchmark to exactly `units` orders, replicating orders under fresh ids.

    Copy 0 keeps the original ids; copy k derives each id from (original id, k). Bank, logistics
    and ground-truth rows follow their order, so every copy reconciles like its original. Rows
    naming no order (orphans) are copied once, unscaled: replicating them would duplicate their keys.
    The output is deterministic and reused when it already exists for the same source and size.
    """
    source_dir, out_dir = Path(source_dir), Path(out_dir)
    stamp = {"version": 2, "units": units, "source": hashlib.sha256(b"".join(
        hashlib.sha256((source_dir / name).read_bytes()).digest() for name in FILES)).hexdigest()}
    marker = out_dir / "scale.json"
    if marker.exists() and json.loads(marker.read_text(encoding="utf-8")) == stamp:
        return out_dir
    read = lambda name: pd.read_csv(source_dir / name, dtype=str, keep_default_na=False)  # noqa: E731
    oms, bank, logistics, truth = (read(name) for name in FILES)
    copies = math.ceil(units / len(oms))
    keys = {"oms": oms["order_id"], "bank": bank["txn_ref"].str.removeprefix("OLIST-PAY-"),
            "logistics": logistics["client_order_code"], "truth": truth["order_id"]}
    feeds = {"bank": bank, "logistics": logistics, "truth": truth}
    named = {name: keys[name].isin(oms["order_id"]) for name in feeds}
    orphans = {name: frame[~named[name]] for name, frame in feeds.items()}
    bank, logistics, truth = (frame[named[name]].reset_index(drop=True) for name, frame in feeds.items())
    keys.update({name: keys[name][named[name]].reset_index(drop=True) for name in feeds})
    out = {name: [] for name in keys}
    for copy in range(copies):
        new = {old: _fresh_id(old, copy) for old in oms["order_id"]}
        swap = lambda frame, key, columns: frame.assign(**{  # noqa: E731
            column: [value.replace(old, new.get(old, old)) if old else value
                     for value, old in zip(frame[column], key)] for column in columns})
        out["oms"].append(swap(oms, keys["oms"], ["order_id", "tracking_no"]))
        out["bank"].append(swap(bank, keys["bank"], ["txn_ref", "description"]))
        out["logistics"].append(swap(logistics, keys["logistics"],
                                     ["tracking_code", "client_order_code", "remittance_id"]))
        out["truth"].append(swap(truth, keys["truth"], ["order_id", "tracking_no", "bank_txn_ref"]))
    frames = {name: pd.concat(parts, ignore_index=True) for name, parts in out.items()}
    keep = set(frames["oms"]["order_id"].iloc[:units])
    frames["oms"] = frames["oms"].iloc[:units]
    kept = {"bank": frames["bank"]["txn_ref"].str.removeprefix("OLIST-PAY-").isin(keep),
            "logistics": frames["logistics"]["client_order_code"].isin(keep),
            "truth": frames["truth"]["order_id"].isin(keep)}
    for name, mask in kept.items():
        frames[name] = pd.concat([frames[name][mask], orphans[name]], ignore_index=True)
    frames["truth"] = frames["truth"].assign(unit_id=lambda t: [str(i) for i in range(1, len(t) + 1)])
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in zip(FILES, (frames["oms"], frames["bank"], frames["logistics"], frames["truth"])):
        frame.to_csv(out_dir / name, index=False)
    marker.write_text(json.dumps(stamp), encoding="utf-8")
    return out_dir


# --- one measurement -------------------------------------------------------------------------

def measure_once(sources: str | Path, config_path: str | Path) -> dict[str, Any]:
    """Inspect, run and export once, as the app does; returns timings, CPU, memory and sizes."""
    from recon.pipeline import inspect, load_config, run

    config = load_config(config_path)
    started = time.perf_counter()
    inspect(sources, config)
    inspected = time.perf_counter()
    outcome = run(sources, config)
    ran = time.perf_counter()
    outcome.bundle()
    exported = time.perf_counter()
    performance = outcome.manifest["performance"]
    return {
        "units": performance["units"],
        "inspect_seconds": inspected - started,
        "run_seconds": ran - inspected,
        "export_seconds": exported - ran,
        "cpu_seconds": time.process_time(),  # whole process: inspect + run + export
        "peak_rss_bytes": process_peak_rss_bytes(),  # a fresh process per sample: its lifetime peak
        "input_bytes": performance["input_bytes"],
        "output_bytes": performance["output_bytes"],
        "stages": performance["stages"],
    }


# --- cost and report -------------------------------------------------------------------------

@dataclass(frozen=True)
class CostModel:
    """Prices are assumptions to replace with your own (defaults: AWS Fargate and S3 Standard, us-east-1)."""

    vcpu_hour: float = 0.04048  # USD per vCPU-hour
    gb_hour: float = 0.004445  # USD per GB of memory per hour
    storage_gb_month: float = 0.023  # USD per GB-month
    retention_months: int = 12  # inputs and outputs kept this long
    api_per_run: float = 0.0  # no external API is called today

    def per_run(self, sample: dict[str, Any]) -> dict[str, float]:
        wall_hours = (sample["inspect_seconds"] + sample["run_seconds"] + sample["export_seconds"]) / 3600
        compute = sample["cpu_seconds"] / 3600 * self.vcpu_hour
        memory = sample["peak_rss_bytes"] / 1e9 * wall_hours * self.gb_hour
        storage = (sample["input_bytes"] + sample["output_bytes"]) / 1e9 * self.storage_gb_month * self.retention_months
        return {"compute": compute, "memory": memory, "storage": storage, "api": self.api_per_run,
                "total": compute + memory + storage + self.api_per_run}


def _subprocess_env() -> dict[str, str]:
    """This environment with our `src` first on PYTHONPATH (no empty entry: it would add the working dir)."""
    paths = [str(Path(__file__).resolve().parents[1]), os.environ.get("PYTHONPATH", "")]
    return {**os.environ, "PYTHONPATH": os.pathsep.join(path for path in paths if path)}


def _measure_in_subprocess(sources: Path, config_path: Path) -> dict[str, Any]:
    done = subprocess.run([sys.executable, "-m", "recon.perf", "once", str(sources), str(config_path)],
                          capture_output=True, text=True, env=_subprocess_env(), check=True)
    return json.loads(done.stdout.strip().splitlines()[-1])


def _pct(values: list[float]) -> tuple[float, float, float]:
    p50, p95, p99 = np.percentile(values, [50, 95, 99])
    return float(p50), float(p95), float(p99)


def _machine() -> str:
    import pandas

    return (f"{platform.system()} {platform.release()}, {platform.processor() or platform.machine()}, "
            f"{os.cpu_count()} logical CPUs, Python {platform.python_version()}, pandas {pandas.__version__}")


def pair_repeats(sizes: list[int], repeats: list[int]) -> list[int]:
    """Repeats per size: one value for every size, or exactly one per size."""
    if len(repeats) == 1:
        return repeats * len(sizes)
    if len(repeats) != len(sizes):
        raise ValueError(f"--repeats needs one value or one per size ({len(sizes)}), got {len(repeats)}")
    return list(repeats)


def report(base_dir: str | Path, sizes: list[int], repeats: list[int], config_path: str | Path,
           work_dir: str | Path, cost: CostModel = CostModel()) -> str:
    """Measure each size `repeats` times (each in a fresh process) and return a markdown report."""
    work_dir, config_path = Path(work_dir), Path(config_path).resolve()
    samples: dict[int, list[dict[str, Any]]] = {}
    for size, count in zip(sizes, pair_repeats(sizes, repeats)):
        scaled = scale_benchmark(base_dir, work_dir / f"scaled_{size}", size)
        samples[size] = [_measure_in_subprocess(scaled, config_path) for _ in range(count)]

    lines = ["# Performance and cost", "",
             f"Measured on {_machine()}. Each sample is a fresh process running inspect, run and export, "
             "as the app does. Percentiles are over the samples listed; with few samples P95/P99 are close "
             "to the maximum.", "",
             "## Reconcile time (`run`: load, checks, funnel, reverse pass)", "",
             "| Units | Samples | P50 (s) | P95 (s) | P99 (s) | Inspect P50 (s) | Export P50 (s) | Matching share | "
             "Peak memory (GB) | Units / s |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for size, runs in samples.items():
        p50, p95, p99 = _pct([r["run_seconds"] for r in runs])
        matching = np.median([sum(v for k, v in r["stages"].items() if k.startswith(MATCHING_PREFIX))
                              / max(r["run_seconds"], 1e-9) for r in runs])
        lines.append(f"| {size:,} | {len(runs)} | {p50:.2f} | {p95:.2f} | {p99:.2f} | "
                     f"{np.median([r['inspect_seconds'] for r in runs]):.2f} | "
                     f"{np.median([r['export_seconds'] for r in runs]):.2f} | {matching:.0%} | "
                     f"{max(r['peak_rss_bytes'] for r in runs) / 1e9:.2f} | {size / p50:,.0f} |")

    largest = max(samples)
    run_stages = [{k: v for k, v in r["stages"].items() if k != "export"} for r in samples[largest]]  # export: not `run`
    stage_medians = pd.DataFrame(run_stages).median().sort_values(ascending=False)
    run_p50 = _pct([r["run_seconds"] for r in samples[largest]])[0]
    lines += ["", f"## Slowest stages at {largest:,} units (median seconds, share of `run`)", "",
              "| Stage | Seconds | Share |", "|---|---|---|"]
    lines += [f"| `{stage}` | {seconds:.2f} | {seconds / run_p50:.0%} |"
              for stage, seconds in stage_medians.head(8).items()]

    lines += ["", "## Cost per reconciliation", "",
              "Assumed prices (replace with your own): " + ", ".join(f"`{k}` = {v}" for k, v in asdict(cost).items())
              + ". Compute is CPU-seconds of the whole process; memory is peak memory held for the wall time; "
              "storage keeps inputs and the export bundle for the retention period. No external API is called.", "",
              "| Units | Compute ($) | Memory ($) | Storage ($) | API ($) | Total per run ($) "
              "| Cost per 1,000 units ($) |",
              "|---|---|---|---|---|---|---|"]
    for size, runs in samples.items():
        costs = pd.DataFrame([cost.per_run(r) for r in runs]).median()
        lines.append(f"| {size:,} | {costs['compute']:.5f} | {costs['memory']:.5f} | {costs['storage']:.5f} | "
                     f"{costs['api']:.5f} | {costs['total']:.5f} | {costs['total'] / size * 1000:.6f} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m recon.perf")
    sub = parser.add_subparsers(dest="command", required=True)
    once = sub.add_parser("once", help="measure one run (prints JSON)")
    once.add_argument("sources")
    once.add_argument("config")
    rep = sub.add_parser("report", help="measure several sizes and write a markdown report")
    rep.add_argument("--base", default="data/generated")
    rep.add_argument("--config", default="configs/olist_benchmark.json")
    rep.add_argument("--sizes", type=int, nargs="+", default=[100_000, 1_000_000])
    rep.add_argument("--repeats", type=int, nargs="+", default=[10, 3],
                     help="samples per size: one value for all sizes, or one per size")
    rep.add_argument("--work", default="data/scaled")
    rep.add_argument("--out", default="reports/performance.md")
    args = parser.parse_args(argv)
    if args.command == "once":
        print(json.dumps(measure_once(args.sources, args.config)))
        return
    try:
        pair_repeats(args.sizes, args.repeats)
    except ValueError as error:
        parser.error(str(error))
    markdown = report(args.base, args.sizes, args.repeats, args.config, args.work)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
