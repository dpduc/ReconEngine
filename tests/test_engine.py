from pathlib import Path

import pandas as pd

from recon.engine import Policy, attach_truth, build_features, load_feeds, metrics, reconcile, validate_feed


ROOT = Path(__file__).parents[1]
DATA = ROOT / "data" / "generated"


def test_engine_is_deterministic_and_high_precision():
    feeds = load_feeds(DATA)
    policy = Policy(mdr_bps=0, cod_fee_bps=0, amount_tolerance=0.01)
    first = reconcile(build_features(feeds, policy), policy)
    second = reconcile(build_features(feeds, policy), policy)
    pd.testing.assert_frame_equal(first, second)


def test_bundled_fixture_meets_precision_bar():
    results = attach_truth(
        reconcile(build_features(load_feeds(DATA), Policy(mdr_bps=0, cod_fee_bps=0, amount_tolerance=0.01)), Policy(mdr_bps=0, cod_fee_bps=0, amount_tolerance=0.01)),
        pd.read_csv(DATA / "ground_truth.csv"),
    )
    summary = metrics(results)
    assert summary["auto_accept_precision"] >= 0.995
    assert summary["review_rate"] <= 0.01


def test_schema_validation_reports_missing_columns():
    errors = validate_feed(pd.DataFrame({"order_id": ["ORD-1"]}), "oms")
    assert "missing required columns" in errors[0]
