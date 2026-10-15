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
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from recon.funnel import MATCHING_GROUPS, STEP_GROUPS
from recon.timing import process_peak_rss_bytes

FILES = ("oms_orders.csv", "bank_statement.csv", "logistics_report.csv", "ground_truth.csv")
FUNNEL_PREFIX = "funnel:"
UNTIMED = "untimed"  # `run` time outside every recorded stage


# --- scaling ---------------------------------------------------------------------------------

def _fresh_id(order_id: str, copy: int) -> str:
    return order_id if copy == 0 else hashlib.md5(f"{order_id}:{copy}".encode()).hexdigest()


def _read(source_dir: Path, name: str) -> pd.DataFrame:
    return pd.read_csv(source_dir / name, dtype=str, keep_default_na=False)


def _rename_ids(frame: pd.DataFrame, key: pd.Series, columns: list[str], new: dict[str, str]) -> pd.DataFrame:
    """Replace each row's order id (`key`) with its new id inside `columns`; rows without an id stay."""
    return frame.assign(**{column: [value.replace(old, new.get(old, old)) if old else value
                                    for value, old in zip(frame[column], key)] for column in columns})


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
    oms, bank, logistics, truth = (_read(source_dir, name) for name in FILES)
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
        out["oms"].append(_rename_ids(oms, keys["oms"], ["order_id", "tracking_no"], new))
        out["bank"].append(_rename_ids(bank, keys["bank"], ["txn_ref", "description"], new))
        out["logistics"].append(_rename_ids(logistics, keys["logistics"],
                                            ["tracking_code", "client_order_code", "remittance_id"], new))
        out["truth"].append(_rename_ids(truth, keys["truth"], ["order_id", "tracking_no", "bank_txn_ref"], new))
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

@dataclass(frozen=True)
class Sample:
    """One measured reconciliation: inspect, run and export in a fresh process."""

    units: int
    inspect_seconds: float
    run_seconds: float
    export_seconds: float
    cpu_seconds: float  # inspect + run + export, not process startup or imports
    peak_rss_bytes: int
    input_bytes: int
    output_bytes: int
    stages: dict[str, float] = field(default_factory=dict)

    @property
    def wall_seconds(self) -> float:
        """What the user waits for: inspect, run and export."""
        return self.inspect_seconds + self.run_seconds + self.export_seconds


def measure_once(sources: str | Path, config_path: str | Path) -> Sample:
    """Inspect, run and export once, as the app does; returns timings, CPU, memory and sizes."""
    from recon.pipeline import inspect, load_config, run

    config = load_config(config_path)
    started, cpu_started = time.perf_counter(), time.process_time()  # after imports and config: the work only
    inspect(sources, config)
    inspected = time.perf_counter()
    outcome = run(sources, config)
    ran = time.perf_counter()
    outcome.bundle()
    exported = time.perf_counter()
    performance = outcome.manifest["performance"]
    return Sample(units=performance["units"], inspect_seconds=inspected - started, run_seconds=ran - inspected,
                  export_seconds=exported - ran, cpu_seconds=time.process_time() - cpu_started,
                  peak_rss_bytes=process_peak_rss_bytes(),  # a fresh process per sample: its lifetime peak
                  input_bytes=performance["input_bytes"], output_bytes=performance["output_bytes"],
                  stages=performance["stages"])


# --- cost and report -------------------------------------------------------------------------

@dataclass(frozen=True)
class CostModel:
    """Prices are assumptions to replace with your own (defaults: AWS Fargate and S3 Standard, us-east-1)."""

    vcpu_hour: float = 0.04048  # USD per vCPU-hour
    gb_hour: float = 0.004445  # USD per GB of memory per hour
    storage_gb_month: float = 0.023  # USD per GB-month
    retention_months: int = 12  # inputs and outputs kept this long
    api_per_run: float = 0.0  # no external API is called today

    def per_run(self, sample: Sample) -> dict[str, float]:
        wall_hours = sample.wall_seconds / 3600
        compute = sample.cpu_seconds / 3600 * self.vcpu_hour
        memory = sample.peak_rss_bytes / 1e9 * wall_hours * self.gb_hour
        storage = (sample.input_bytes + sample.output_bytes) / 1e9 * self.storage_gb_month * self.retention_months
        return {"compute": compute, "memory": memory, "storage": storage, "api": self.api_per_run,
                "total": compute + memory + storage + self.api_per_run}


COST_COMPONENTS = ("compute", "memory", "storage", "api")


def _dominance(units: int, costs: pd.Series, cost: CostModel) -> str:
    """One sentence naming the component that dominates the cost per run."""
    component = max(COST_COMPONENTS, key=lambda name: costs[name])
    sentence = f"At {units:,} units, {component} is {costs[component] / costs['total']:.0%} of the cost per run"
    if component == "storage":
        return (sentence + f"; it follows the assumed retention of {cost.retention_months} months, so compare "
                "compute + memory per 1,000 units for the cost of reconciling itself.")
    return sentence + "."


def _subprocess_env() -> dict[str, str]:
    """This environment with our `src` first on PYTHONPATH (no empty entry: it would add the working dir)."""
    paths = [str(Path(__file__).resolve().parents[1]), os.environ.get("PYTHONPATH", "")]
    return {**os.environ, "PYTHONPATH": os.pathsep.join(path for path in paths if path)}


def _measure_in_subprocess(sources: Path, config_path: Path) -> Sample:
    done = subprocess.run([sys.executable, "-m", "recon.perf", "once", str(sources), str(config_path)],
                          capture_output=True, text=True, env=_subprocess_env(), check=True)
    return Sample(**json.loads(done.stdout.strip().splitlines()[-1]))


def _pct(values: list[float]) -> tuple[float, float, float]:
    p50, p95, p99 = np.percentile(values, [50, 95, 99])
    return float(p50), float(p95), float(p99)


def _median(values: list[float]) -> float:
    return _pct(values)[0]


def _median_costs(runs: list[Sample], cost: CostModel) -> pd.Series:
    return pd.DataFrame([cost.per_run(r) for r in runs]).median()


def _median_units(runs: list[Sample]) -> int:
    return int(np.median([r.units for r in runs]))


def _machine() -> str:
    return (f"{platform.system()} {platform.release()}, {platform.processor() or platform.machine()}, "
            f"{os.cpu_count()} logical CPUs, Python {platform.python_version()}, pandas {pd.__version__}")


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
    samples: dict[int, list[Sample]] = {}
    for size, count in zip(sizes, pair_repeats(sizes, repeats)):
        scaled = scale_benchmark(base_dir, work_dir / f"scaled_{size}", size)
        samples[size] = [_measure_in_subprocess(scaled, config_path) for _ in range(count)]

    return render_report(samples, cost)


def stage_group(stage: str) -> str:
    """A funnel step's group (see `recon.funnel.STEP_GROUPS`); any other stage is its own group."""
    return STEP_GROUPS[stage.removeprefix(FUNNEL_PREFIX)] if stage.startswith(FUNNEL_PREFIX) else stage


def run_groups(sample: Sample) -> dict[str, float]:
    """Seconds of `run` per group, with the unrecorded remainder as `untimed`; export is not part of `run`."""
    groups: dict[str, float] = {}
    for stage, seconds in sample.stages.items():
        if stage != "export":
            groups[stage_group(stage)] = groups.get(stage_group(stage), 0.0) + seconds
    groups[UNTIMED] = max(sample.run_seconds - sum(groups.values()), 0.0)
    return groups


def matching_share(sample: Sample) -> float:
    """Linking + rule-check steps over the wall time of `run`."""
    groups = run_groups(sample)
    return sum(groups.get(group, 0.0) for group in MATCHING_GROUPS) / max(sample.run_seconds, 1e-9)


def _pct_cells(values: list[float]) -> str:
    return " | ".join(f"{value:.3f}" for value in _pct(values))


def render_report(samples: dict[int, list[Sample]], cost: CostModel = CostModel()) -> str:
    """The markdown report for samples per size, as `measure_once` returns them."""
    lines = ["# Performance and cost", "",
             f"Measured on {_machine()}. Each sample is a fresh process running inspect, run and export, "
             "as the app does. Percentiles are over the samples listed; with few samples P95/P99 are close "
             "to the maximum.", "",
             "## Time per reconciliation (seconds)", "",
             "Overall is what the user waits for: inspect + run + export. `run` is load, checks, funnel "
             "and reverse pass.", "",
             "| Units | Samples | Measure | P50 | P95 | P99 |", "|---|---|---|---|---|---|"]
    measures = {"Overall": "wall_seconds", "Inspect": "inspect_seconds", "Run": "run_seconds",
                "Export": "export_seconds"}
    for size, runs in samples.items():
        lines += [f"| {size:,} | {len(runs)} | {name} | {_pct_cells([getattr(r, measure) for r in runs])} |"
                  for name, measure in measures.items()]

    lines += ["", "## Throughput and memory", "",
              "Matching share is the time of the funnel's linking and rule-check steps over the wall time "
              "of `run` (median over samples); the decision and policy/routing steps are not matching.", "",
              "| Units | Matching share | Peak memory (GB) | Units / s (run P50) | Units / s (overall P50) |",
              "|---|---|---|---|---|"]
    for size, runs in samples.items():
        matching = np.median([matching_share(r) for r in runs])
        run_p50, wall_p50 = _median([r.run_seconds for r in runs]), _median([r.wall_seconds for r in runs])
        lines.append(f"| {size:,} | {matching:.0%} | {max(r.peak_rss_bytes for r in runs) / 1e9:.2f} | "
                     f"{size / run_p50:,.0f} | {size / wall_p50:,.0f} |")

    lines += ["", "## Where `run` time goes (median share of `run`)", "",
              "Funnel steps are grouped as in `recon.funnel.STEP_GROUPS`; other stages are listed by name, "
              f"and `{UNTIMED}` is `run` time outside every recorded stage.", "",
              "| Units | Group | Share |", "|---|---|---|"]
    for size, runs in samples.items():
        shares = pd.DataFrame([{group: seconds / max(r.run_seconds, 1e-9) for group, seconds in run_groups(r).items()}
                               for r in runs]).fillna(0.0).median()
        lines += [f"| {size:,} | `{group}` | {share:.0%} |" for group, share in shares.items()]

    largest = max(samples)
    run_stages = [{k: v for k, v in r.stages.items() if k != "export"} for r in samples[largest]]  # export: not `run`
    stage_medians = pd.DataFrame(run_stages).median().sort_values(ascending=False)
    run_p50 = _median([r.run_seconds for r in samples[largest]])
    lines += ["", f"## Slowest stages at {largest:,} units (median seconds, share of `run`)", "",
              "| Stage | Seconds | Share |", "|---|---|---|"]
    lines += [f"| `{stage}` | {seconds:.2f} | {seconds / run_p50:.0%} |"
              for stage, seconds in stage_medians.head(8).items()]

    lines += ["", "## Cost per reconciliation", "",
              "Assumed prices (replace with your own): " + ", ".join(f"`{k}` = {v}" for k, v in asdict(cost).items())
              + ". Compute is the CPU-seconds of inspect, run and export (not process startup or imports); "
              "memory is the peak memory held for the wall time; storage keeps the inputs and the export bundle "
              "for the retention period, which is an assumption. No external API is called. Costs are medians "
              "over samples; per 1,000 units divides by the units each sample measured.", "",
              "| Units | Compute ($) | Memory ($) | Storage ($) | API ($) | Total per run ($) "
              "| Compute + memory per 1,000 units ($) | Storage per 1,000 units ($) | Total per 1,000 units ($) "
              "| Compute / memory / storage share |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for size, runs in samples.items():
        costs = _median_costs(runs, cost)
        per_1000 = pd.DataFrame([{name: value / max(r.units, 1) * 1000 for name, value in cost.per_run(r).items()}
                                 for r in runs]).median()
        units = _median_units(runs)
        shares = " / ".join(f"{costs[name] / costs['total']:.0%}" for name in ("compute", "memory", "storage"))
        lines.append(f"| {units:,} | {costs['compute']:.5f} | {costs['memory']:.5f} | {costs['storage']:.5f} | "
                     f"{costs['api']:.5f} | {costs['total']:.5f} | {per_1000['compute'] + per_1000['memory']:.6f} | "
                     f"{per_1000['storage']:.6f} | {per_1000['total']:.6f} | {shares} |")
    lines += ["", _dominance(_median_units(samples[largest]), _median_costs(samples[largest], cost), cost)]

    lines += ["", "## Appendix: time per stage (seconds)", "",
              "Stages in pipeline order; percentiles are over the samples that ran the stage.", "",
              "| Units | Stage | P50 | P95 | P99 |", "|---|---|---|---|---|"]
    for size, runs in samples.items():
        stages = list(dict.fromkeys(stage for r in runs for stage in r.stages))
        lines += [f"| {size:,} | `{stage}` | {_pct_cells([r.stages[stage] for r in runs if stage in r.stages])} |"
                  for stage in stages]
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
        print(json.dumps(asdict(measure_once(args.sources, args.config))))
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
