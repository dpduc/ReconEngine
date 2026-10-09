from pathlib import Path

import pandas as pd

from recon.engine import Policy, attach_truth, build_features, load_feeds, metrics, reconcile, validate_feed


ROOT = Path(__file__).parents[1]
DATA = ROOT / "data" / "generated"


def test_engine_is_deterministic_and_high_precision():
    feeds = load_feeds(DATA)
    first = reconcile(build_features(feeds), Policy())
    second = reconcile(build_features(feeds), Policy())
    pd.testing.assert_frame_equal(first, second)


def test_bundled_fixture_meets_precision_bar():
    results = attach_truth(
        reconcile(build_features(load_feeds(DATA)), Policy()),
        pd.read_csv(DATA / "ground_truth.csv"),
    )
    summary = metrics(results)
    assert summary["auto_accept_precision"] >= 0.995
    assert summary["review_rate"] <= 0.01


def test_schema_validation_reports_missing_columns():
    errors = validate_feed(pd.DataFrame({"order_id": ["ORD-1"]}), "oms")
    assert "missing required columns" in errors[0]
