"""Scoring unit results against ground truth, and summary metrics."""

from __future__ import annotations

from typing import Any

import pandas as pd


def attach_truth(results: pd.DataFrame, truth: pd.DataFrame) -> pd.DataFrame:
    truth = truth.drop_duplicates("order_id").rename(
        columns={"expected_outcome": "truth_outcome", "expected_exception": "truth_exception"}
    )
    out = results.merge(truth[["order_id", "scenario", "sub_kind", "truth_outcome", "truth_exception"]], on="order_id", how="left")
    out["truth_decision"] = out["truth_outcome"].map(
        lambda value: "AUTO_ACCEPT" if value in {"PASS_1", "PASS_2"} else "EXCEPTION"
    )
    out["is_correct"] = out["decision"].eq(out["truth_decision"])
    return out


def metrics(results: pd.DataFrame) -> dict[str, Any]:
    accepted = results[results["decision"] == "AUTO_ACCEPT"]
    reviewed = results[results["decision"] == "REVIEW"]
    truth_available = results["truth_decision"].notna() if "truth_decision" in results else pd.Series(False, index=results.index)
    scored = results[truth_available]
    return {
        "rows": int(len(results)),
        "auto_accepts": int(len(accepted)),
        "review_queue": int(len(reviewed)),
        "review_rate": float(len(reviewed) / len(results)) if len(results) else 0.0,
        "auto_accept_precision": float(accepted["is_correct"].mean()) if len(accepted) and "is_correct" in accepted else None,
        "accuracy": float(scored["is_correct"].mean()) if len(scored) else None,
        "scenario_counts": {k: int(v) for k, v in results["reason_codes"].value_counts().head(12).items()},
    }
