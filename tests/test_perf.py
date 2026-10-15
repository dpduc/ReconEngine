"""Runtime and cost measurement: per-stage timings in every manifest, scaled benchmarks, the report."""

import json
import os
import shutil
from pathlib import Path

import pandas as pd
import pytest

from recon.olistdata import generate_from_olist
from recon.funnel import STEP_GROUPS, STEPS
from recon.perf import (CostModel, _subprocess_env, main, matching_share, pair_repeats, render_report, report,
                        run_groups, scale_benchmark)
from recon.pipeline import load_config, run
from recon.timing import RunMeter, process_peak_rss_bytes

from test_pipeline import OMS, write_sources

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


def test_export_records_its_stage_and_output_size(tmp_path):
    outcome = run(tmp_path, write_sources(tmp_path))

    files = outcome.bundle()

    performance = outcome.manifest["performance"]
    assert performance["stages"]["export"] >= 0
    assert performance["output_bytes"] == sum(len(text.encode("utf-8")) for name, text in files.items()
                                              if name != "manifest.json") > 0
    assert json.loads(files["manifest.json"])["performance"]["output_bytes"] == performance["output_bytes"]
    assert "export" not in run(tmp_path, write_sources(tmp_path)).manifest["performance"]["stages"]


def test_a_blocked_run_records_the_stages_that_ran(tmp_path):
    outcome = run(tmp_path, write_sources(tmp_path, oms=OMS.replace("Amount", "Total")))

    assert outcome.blocked
    performance = outcome.manifest["performance"]
    assert set(performance["stages"]) == {"load", "resolutions", "raw_checks", "parse_numeric", "checks", "profiles"}
    assert performance["units"] == 0 and performance["input_bytes"] > 0 and performance["peak_rss_bytes"] > 0


def test_a_small_run_after_a_large_one_reports_its_own_peak(tmp_path):
    with RunMeter() as large:
        ballast = b"x" * 400_000_000  # written, so it is resident
        large_peak = large.peak_rss_bytes()
        del ballast

    performance = run(tmp_path, write_sources(tmp_path)).manifest["performance"]

    assert performance["process_peak_rss_bytes"] >= large_peak
    assert 0 < performance["peak_rss_bytes"] < large_peak - 300_000_000


def test_a_run_that_sets_a_new_high_reports_the_lifetime_peak():
    with RunMeter() as meter:
        ballast = b"x" * (process_peak_rss_bytes() + 100_000_000)  # above any earlier peak of this process
        del ballast
        performance = meter.performance(units=0, input_bytes=0)

    assert performance["peak_rss_bytes"] == performance["process_peak_rss_bytes"]


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

    for heading in ("## Time per reconciliation", "## Throughput and memory", "## Slowest stages",
                    "## Cost per reconciliation", "## Appendix: time per stage", "Matching share", "Peak memory"):
        assert heading in markdown
    for size, samples in (("300", 3), ("600", 2)):
        assert all(f"| {size} | {samples} | {measure} |" in markdown for measure in ("Overall", "Inspect", "Run", "Export"))
        assert f"| {size} | `funnel:link_exact` |" in markdown and f"| {size} | `export` |" in markdown


def _sample(seconds: float, **stages: float) -> dict:
    return {"units": 10, "inspect_seconds": seconds, "run_seconds": 2 * seconds, "export_seconds": seconds,
            "cpu_seconds": seconds, "peak_rss_bytes": 10**8, "input_bytes": 10**6, "output_bytes": 10**6,
            "stages": stages}


def test_report_gives_p50_p95_p99_overall_and_per_stage():
    runs = [_sample(s, load=s, export=s / 10) for s in (1.0, 2.0, 3.0, 4.0, 5.0)] + [_sample(6.0, load=6.0)]

    lines = render_report({1000: runs}).splitlines()

    assert "| 1,000 | 6 | Overall | 14.000 | 23.000 | 23.800 |" in lines  # 4 x 1..6
    assert "| 1,000 | 6 | Run | 7.000 | 11.500 | 11.900 |" in lines
    assert "| 1,000 | `load` | 3.500 | 5.750 | 5.950 |" in lines
    assert "| 1,000 | `export` | 0.300 | 0.480 | 0.496 |" in lines  # only five samples ran it


def test_every_funnel_step_has_a_group():
    assert set(STEP_GROUPS) == {step.name for step in STEPS}


def test_matching_share_is_linking_and_rule_checks_over_run():
    sample = _sample(1.0, load=0.2, **{"funnel:link_exact": 0.3, "funnel:amount_mismatch": 0.1,
                                       "funnel:decision": 0.5, "funnel:status_policy": 0.4, "export": 9.0})

    assert matching_share(sample) == pytest.approx(0.4 / 2.0)
    assert run_groups(sample) == pytest.approx({"load": 0.2, "linking": 0.3, "rule checks": 0.1, "decision": 0.5,
                                                "policy/routing": 0.4, "untimed": 0.5})
    markdown = render_report({10: [sample]})
    assert "linking and rule-check steps over the wall time of `run`" in markdown
    assert "| 10 | `decision` | 25% |" in markdown and "| 10 | `untimed` | 25% |" in markdown


def test_scaling_copies_orphan_rows_once(small_benchmark, tmp_path):
    source = tmp_path / "source"
    shutil.copytree(small_benchmark, source)
    for name, column, value in (("bank_statement.csv", "txn_ref", "OLIST-PAY-" + "f" * 32),
                                ("logistics_report.csv", "tracking_code", "ORPHAN-SHIPMENT")):
        frame = pd.read_csv(source / name, dtype=str, keep_default_na=False)
        orphan = frame.iloc[[0]].assign(**{column: value})
        if name == "logistics_report.csv":
            orphan = orphan.assign(client_order_code="NO-SUCH-ORDER")
        pd.concat([frame, orphan]).to_csv(source / name, index=False)

    scaled = scale_benchmark(source, tmp_path / "scaled", units=700)

    bank = pd.read_csv(scaled / "bank_statement.csv", dtype=str)
    logistics = pd.read_csv(scaled / "logistics_report.csv", dtype=str)
    assert (bank["txn_ref"] == "OLIST-PAY-" + "f" * 32).sum() == 1
    assert (logistics["tracking_code"] == "ORPHAN-SHIPMENT").sum() == 1
    assert bank["txn_ref"].is_unique and logistics["tracking_code"].is_unique


def test_one_repeats_value_applies_to_every_size_and_a_mismatch_fails():
    assert pair_repeats([100, 200, 300], [5]) == [5, 5, 5]
    assert pair_repeats([100, 200], [10, 3]) == [10, 3]
    with pytest.raises(ValueError, match="one per size"):
        pair_repeats([100, 200, 300], [10, 3])
    with pytest.raises(SystemExit):  # the CLI refuses before measuring anything
        main(["report", "--sizes", "100", "200", "300", "--repeats", "10", "3"])


@pytest.mark.parametrize("existing", [None, "", "elsewhere"])
def test_subprocess_path_has_no_empty_entry(monkeypatch, existing):
    if existing is None:
        monkeypatch.delenv("PYTHONPATH", raising=False)
    else:
        monkeypatch.setenv("PYTHONPATH", existing)

    entries = _subprocess_env()["PYTHONPATH"].split(os.pathsep)

    assert all(entries) and entries[0] == str(ROOT / "src")
    assert entries[1:] == ([existing] if existing else [])
