"""Runtime and cost measurement: per-stage timings in every manifest, scaled benchmarks, the report."""

from pathlib import Path

import pandas as pd
import pytest

from recon.olistdata import generate_from_olist
from recon.perf import CostModel, report, scale_benchmark
from recon.pipeline import load_config, run

from test_pipeline import write_sources

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "configs" / "olist_benchmark.json"


def test_every_run_records_stage_timings_cpu_and_peak_memory(tmp_path):
    config = write_sources(tmp_path)

    performance = run(tmp_path, config).manifest["performance"]

    stages = performance["stages"]
    assert {"load", "resolutions", "raw_checks", "parse_numeric", "checks", "profiles", "reverse_pass"} <= set(stages)
    assert {f"funnel:{step}" for step in ("link_exact", "amount_mismatch", "decision")} <= set(stages)
    assert all(seconds >= 0 for seconds in stages.values())
    assert performance["total_seconds"] >= sum(stages.values()) * 0.99
    assert performance["cpu_seconds"] > 0 and performance["peak_rss_bytes"] > 0
    assert performance["units"] == 3
    assert performance["input_bytes"] == sum((tmp_path / spec.path).stat().st_size for spec in config.feeds.values())


@pytest.fixture(scope="module")
def small_benchmark(tmp_path_factory):
    folder = tmp_path_factory.mktemp("olist")
    generate_from_olist(ROOT / "brazil ecom", folder, rows=300, seed=7)
    return folder


def test_scaled_benchmark_replicates_orders_with_fresh_ids(small_benchmark, tmp_path):
    scaled = scale_benchmark(small_benchmark, tmp_path / "scaled", units=1000)

    oms = pd.read_csv(scaled / "oms_orders.csv")
    assert len(oms) == 1000 and oms["order_id"].is_unique
    assert oms["order_id"].str.fullmatch(r"[0-9a-f]{32}").all()
    base, again = run(small_benchmark, load_config(CONFIG)), run(scaled, load_config(CONFIG))
    assert again.health.gate_open
    assert len(again.results) == 1000 and again.orphans.empty
    # Every copy reconciles exactly like the original orders it was made from.
    share = lambda r: r.results["reason_codes"].value_counts(normalize=True).round(2).to_dict()  # noqa: E731
    assert abs(share(again).get("THREE_WAY_TIE_OUT", 0) - share(base).get("THREE_WAY_TIE_OUT", 0)) <= 0.02
    assert scale_benchmark(small_benchmark, tmp_path / "again", units=1000).joinpath("oms_orders.csv").read_bytes() \
        == (scaled / "oms_orders.csv").read_bytes()


def test_report_gives_percentiles_matching_share_and_cost(small_benchmark, tmp_path):
    markdown = report(base_dir=small_benchmark, sizes=[300, 600], repeats=[3, 2], config_path=CONFIG,
                      work_dir=tmp_path, cost=CostModel())

    for heading in ("P50", "P95", "P99", "Matching share", "Peak memory", "Cost per 1,000 units", "Slowest stages"):
        assert heading in markdown
    assert "| 300 |" in markdown and "| 600 |" in markdown
