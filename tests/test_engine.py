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


def test_benchmark_report_surfaces_known_findings():
    report = inspect(DATA, load_config(CONFIG))
    rows = {issue.id: issue.affected_rows for issue in report.issues}
    assert rows["missing_amount:oms:order_amount"] == 775
    assert rows["zero_amount:bank:credit_amount"] == 3
    assert rows["picked_after_delivered:logistics"] == 30
    assert rows["placeholder_timestamp:logistics:delivered_at:status=SHIPPED"] == 1100
    assert report.profiles["value_date_lag_days"]["p99"] == 4
    # Every finding is acknowledged in the benchmark config, so the gate is open.
    assert all(report.is_acknowledged(issue) for issue in report.issues)


def test_engine_is_deterministic(benchmark_run):
    again = run(DATA, load_config(CONFIG))
    pd.testing.assert_frame_equal(benchmark_run.results, again.results)
    pd.testing.assert_frame_equal(benchmark_run.funnel, again.funnel)


def test_funnel_counts_match_the_notebook_funnel(benchmark_run):
    # Section 4 of notebooks/eda_reconciliation_4_tables.ipynb.
    decided = benchmark_run.funnel.set_index("step")["decided"]
    assert decided["missing_source_evidence"] == 775
    assert decided["status_policy"] == 1082
    assert decided["missing_payment"] == 477
    # The notebook's `delta.gt(0.01)` also flags 66 MATCHED units whose delta is exactly
    # 0.01 BRL plus float noise; the engine treats the tolerance as inclusive. Of the rest, the
    # 243 whose delta is the installment interest the statement reports tie out with a note.
    assert decided["amount_mismatch"] == 1340 - 66 - 243
    assert decided["missing_shipment"] == 471
    assert decided["fee_mismatch"] == decided["cod_fee_mismatch"] == 0
    # The notebook stops at "PASS"; timing runs after it: the 30 picked-after-delivered shipments.
    assert decided["timing_mismatch"] == 30
    assert decided["possible_installment_interest"] == 0  # the statement reports interest
    assert decided["decision"] == 95296 + 66 - 30 + 243
    assert decided.drop("reverse_pass").sum() == len(benchmark_run.results)


def test_bundled_fixture_meets_precision_bar(benchmark_run):
    summary = benchmark_run.manifest["metrics"]
    assert summary["auto_accept_precision"] >= 0.995
    assert summary["review_rate"] <= 0.01
    per_code = summary["per_reason_code"]
    assert per_code["AMOUNT_MISMATCH"]["precision"] >= 0.97
    assert per_code["AMOUNT_MISMATCH"]["recall"] >= 0.99  # no injected error hides behind interest
    assert per_code["INSTALLMENT_INTEREST"]["precision"] >= 0.97


def test_installment_interest_forms_the_close_watchlist(benchmark_run):
    watchlist = benchmark_run.close_watchlist
    assert len(watchlist) == benchmark_run.manifest["metrics"]["close_watchlist"]["units"] > 0
    assert set(watchlist["reason_codes"]) == {"INSTALLMENT_INTEREST"}
    assert watchlist["note_amount"].is_monotonic_decreasing


def test_benchmark_links_only_through_pass_1(benchmark_run):
    results = benchmark_run.results
    assert set(results["bank_link_pass"].dropna()) == {"exact"}
    assert not results["deciding_step"].isin(["link_constrained", "link_candidates"]).any()


def test_benchmark_has_no_orphans(benchmark_run):
    # Every generated bank and logistics record names an Olist order, decided or not.
    assert benchmark_run.orphans.empty
