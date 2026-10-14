"""Scoring unit results against ground truth, and summary metrics."""

from __future__ import annotations

from typing import Any

import pandas as pd

from recon.schema import AUTO_ACCEPT, EXCEPTION, REVIEW


def attach_truth(results: pd.DataFrame, truth: pd.DataFrame) -> pd.DataFrame:
    truth = truth.drop_duplicates("order_id").rename(
        columns={"expected_outcome": "truth_outcome", "expected_exception": "truth_exception"}
    )
    out = results.merge(truth[["order_id", "scenario", "sub_kind", "truth_outcome", "truth_exception"]], on="order_id", how="left")
    out["truth_decision"] = out["truth_outcome"].map(
        lambda value: AUTO_ACCEPT if value in {"PASS_1", "PASS_2"} else EXCEPTION
    )
    out["is_correct"] = out["decision"].eq(out["truth_decision"])
    return out


CLEAN_CODES = {"THREE_WAY_TIE_OUT"}  # reason codes that describe a match, not a problem


def _codes(value: Any) -> set[str]:
    return {c for c in str(value).split("|") if c and c not in CLEAN_CODES} if isinstance(value, str) else set()


def per_reason_code(results: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Precision/recall of each predicted reason code against `truth_exception`, on rows with truth."""
    if "truth_outcome" not in results:
        return {}
    scored = results[results["truth_outcome"].notna()]
    predicted = [_codes(v) for v in scored["reason_codes"]]
    actual = [_codes(v) for v in scored["truth_exception"]]
    out: dict[str, dict[str, Any]] = {}
    for code in sorted(set().union(*predicted, *actual)):
        n_pred = sum(code in p for p in predicted)
        n_true = sum(code in a for a in actual)
        hits = sum(code in p and code in a for p, a in zip(predicted, actual))
        out[code] = {"predicted": n_pred, "actual": n_true, "true_positives": hits,
                     "precision": hits / n_pred if n_pred else None,
                     "recall": hits / n_true if n_true else None}
    return out


def metrics(results: pd.DataFrame) -> dict[str, Any]:
    accepted = results[results["decision"] == AUTO_ACCEPT]
    reviewed = results[results["decision"] == REVIEW]
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
        "per_reason_code": per_reason_code(results),
    }
