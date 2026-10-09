"""Precision-first reconciliation engine and explainability artifacts."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from recon.contracts import AMOUNT_TOLERANCE, COD_FEE_BPS, MDR_BPS, WINDOW_DAYS, fee


EXPECTED_COLUMNS = {
    "oms": {"order_id", "tracking_no", "payment_method", "order_amount", "ordered_at"},
    "bank": {"txn_ref", "description", "credit_amount", "fee_amount", "net_amount", "value_date"},
    "logistics": {"tracking_code", "client_order_code", "cod_collected", "ship_fee", "cod_fee"},
}


@dataclass(frozen=True)
class Policy:
    accept_threshold: float = 0.995
    review_threshold: float = 0.80
    max_review_rate: float = 0.01


def validate_feed(frame: pd.DataFrame, feed: str) -> list[str]:
    missing = sorted(EXPECTED_COLUMNS[feed] - set(frame.columns))
    return [f"{feed}: missing required columns: {', '.join(missing)}"] if missing else []


def load_feeds(folder: str | Path) -> dict[str, pd.DataFrame]:
    folder = Path(folder)
    feeds = {
        "oms": pd.read_csv(folder / "oms_orders.csv"),
        "bank": pd.read_csv(folder / "bank_statement.csv"),
        "logistics": pd.read_csv(folder / "logistics_report.csv"),
    }
    errors = [error for name, frame in feeds.items() for error in validate_feed(frame, name)]
    if errors:
        raise ValueError("; ".join(errors))
    return feeds


def _first_by(frame: pd.DataFrame, key: str) -> pd.DataFrame:
    return frame.drop_duplicates(key, keep="first").copy()


def _extract_order_id(description: Any) -> str | None:
    match = re.search(r"(ORD\d{4}-\d{7})", str(description))
    return match.group(1) if match else None


def build_features(feeds: dict[str, pd.DataFrame]) -> pd.DataFrame:
    oms = _first_by(feeds["oms"], "order_id")
    bank = feeds["bank"].copy()
    bank["order_id"] = bank["description"].map(_extract_order_id)
    bank = _first_by(bank.dropna(subset=["order_id"]), "order_id")
    logistics = _first_by(feeds["logistics"], "client_order_code")
    result = oms.merge(bank, on="order_id", how="left", suffixes=("_oms", "_bank"))
    result = result.merge(
        logistics,
        left_on="order_id",
        right_on="client_order_code",
        how="left",
        suffixes=("", "_logistics"),
    )
    result["bank_found"] = result["txn_ref"].notna()
    result["shipment_found"] = result["tracking_code"].notna()
    result["tracking_exact"] = result["tracking_no"].fillna("").eq(result["tracking_code"].fillna(""))
    result["amount_delta"] = result["credit_amount"].fillna(0) - result["order_amount"].fillna(0)
    result["cod_amount_delta"] = result["cod_collected"].fillna(0) - result["order_amount"].fillna(0)
    result["expected_credit"] = (
        result["order_amount"].fillna(0)
        .where(result["payment_method"].eq("PREPAID"), result["cod_collected"].fillna(0)
               - result["ship_fee"].fillna(0) - result["cod_fee"].fillna(0))
    )
    result["credit_delta"] = result["credit_amount"].fillna(0) - result["expected_credit"]
    result["expected_mdr"] = result["credit_amount"].fillna(0).where(
        result["payment_method"].eq("PREPAID"), 0
    ).map(lambda x: fee(int(x), MDR_BPS))
    result["expected_cod_fee"] = result["cod_collected"].fillna(0).map(lambda x: fee(int(x), COD_FEE_BPS))
    result["fee_delta"] = result["fee_amount"].fillna(0) - result["expected_mdr"]
    result["cod_fee_delta"] = result["cod_fee"].fillna(0) - result["expected_cod_fee"]
    result["amount_within_tolerance"] = (
        result["credit_delta"].abs() <= AMOUNT_TOLERANCE
        ) & (~result["payment_method"].eq("COD") | (result["cod_amount_delta"].abs() <= AMOUNT_TOLERANCE))
    result["fee_within_tolerance"] = result["fee_delta"].abs() <= AMOUNT_TOLERANCE
    result["cod_fee_within_tolerance"] = result["cod_fee_delta"].abs() <= AMOUNT_TOLERANCE
    result["candidate_key_exact"] = result["bank_found"] & result["shipment_found"] & result["tracking_exact"]
    result["reason_codes"] = result.apply(_reason_codes, axis=1)
    return result


def _reason_codes(row: pd.Series) -> str:
    reasons: list[str] = []
    if not row["bank_found"]:
        reasons.append("MISSING_PAYMENT")
    if not row["shipment_found"]:
        reasons.append("MISSING_SHIPMENT")
    if row["bank_found"] and not row["amount_within_tolerance"]:
        reasons.append("AMOUNT_MISMATCH")
    if row["bank_found"] and not row["fee_within_tolerance"]:
        reasons.append("FEE_MISMATCH")
    if row["shipment_found"] and not row["cod_fee_within_tolerance"]:
        reasons.append("COD_FEE_MISMATCH")
    if not reasons:
        reasons.append("THREE_WAY_TIE_OUT")
    return "|".join(reasons)


def reconcile(features: pd.DataFrame, policy: Policy = Policy()) -> pd.DataFrame:
    out = features.copy()
    clean = (
        out["bank_found"] & out["shipment_found"] & out["tracking_exact"]
        & out["amount_within_tolerance"] & out["fee_within_tolerance"]
        & out["cod_fee_within_tolerance"]
    )
    out["confidence"] = clean.astype(float).replace({1.0: 0.9999, 0.0: 0.15})
    out["decision"] = "REVIEW"
    out.loc[out["confidence"] >= policy.accept_threshold, "decision"] = "AUTO_ACCEPT"
    out.loc[(~clean) & (out["reason_codes"].str.contains("MISSING|MISMATCH")), "decision"] = "EXCEPTION"
    out["explanation"] = out.apply(
        lambda r: f"{r['reason_codes'].replace('|', ', ')}; confidence={r['confidence']:.4f}",
        axis=1,
    )
    return out


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
        "scenario_counts": results["reason_codes"].value_counts().head(12).to_dict(),
    }


def artifact_bundle(results: pd.DataFrame) -> dict[str, str]:
    export = results.copy()
    export["reason_codes"] = export["reason_codes"].astype(str)
    return {
        "decisions.csv": export.to_csv(index=False),
        "triage_queue.csv": export[export["decision"] == "REVIEW"].to_csv(index=False),
        "metrics.json": json.dumps(metrics(results), indent=2, default=str),
    }
