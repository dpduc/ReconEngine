"""End-to-end regression on the Olist benchmark through the public pipeline."""

from pathlib import Path

import pandas as pd
import pytest

from recon.config import load_config
from recon.pipeline import inspect, run


ROOT = Path(__file__).parents[1]
DATA = ROOT / "data" / "generated"
CONFIG = ROOT / "configs" / "olist_benchmark.json"


@pytest.fixture(scope="module")
def benchmark_run():
    return run(DATA, load_config(CONFIG))


def test_benchmark_passes_the_gate():
    assert inspect(DATA, load_config(CONFIG)).gate_open


def test_engine_is_deterministic(benchmark_run):
    again = run(DATA, load_config(CONFIG))
    pd.testing.assert_frame_equal(benchmark_run.results, again.results)
    pd.testing.assert_frame_equal(benchmark_run.funnel, again.funnel)


def test_bundled_fixture_meets_precision_bar(benchmark_run):
    summary = benchmark_run.manifest["metrics"]
    assert summary["auto_accept_precision"] >= 0.995
    assert summary["review_rate"] <= 0.01
