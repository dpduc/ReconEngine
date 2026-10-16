"""Build a controlled reconciliation benchmark from real Olist records.

Olist contains OMS-like orders, order items, payments, and delivery events, but
not bank statements or 3PL exports. This module derives those two feed shapes
from the real records and records the provenance in ``manifest.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

import pandas as pd


DELTA_TOLERANCE = 0.01 + 1e-9  # same absolute tolerance the engine uses by default


def generate_from_olist(
    source_dir: str | Path,
    out_dir: str | Path,
    *,
    seed: int = 42,
    rows: int | None = None,
) -> dict:
    source_dir, out_dir = Path(source_dir), Path(out_dir)
    orders = pd.read_csv(source_dir / "olist_orders_dataset.csv", parse_dates=[
        "order_purchase_timestamp", "order_approved_at",
        "order_delivered_customer_date", "order_estimated_delivery_date",
    ])
    items = pd.read_csv(source_dir / "olist_order_items_dataset.csv")
    payments = pd.read_csv(source_dir / "olist_order_payments_dataset.csv")
    item_totals = items.groupby("order_id", as_index=False).agg(
        item_total=("price", "sum"), freight_total=("freight_value", "sum")
    )
    payment_totals = payments.groupby("order_id", as_index=False).agg(
        payment_total=("payment_value", "sum"),
        installments=("payment_installments", "max"),
        payment_type=("payment_type", lambda types: "+".join(sorted(set(types)))),
    )
    eligible = orders.merge(item_totals, on="order_id", how="left")
    eligible = eligible.merge(payment_totals, on="order_id", how="left")
    eligible["order_amount"] = (eligible["item_total"] + eligible["freight_total"]).round(2)
    eligible = eligible.sort_values(["order_purchase_timestamp", "order_id"]).copy()
    if rows is not None:
        if rows < 1:
            raise ValueError("rows must be positive when provided")
        eligible = eligible.head(rows)
    rng = random.Random(seed)

    truth_rows: list[dict] = []
    oms_rows: list[dict] = []
    bank_rows: list[dict] = []
    logistics_rows: list[dict] = []
    link_rows: list[dict] = []
    for unit_id, row in enumerate(eligible.itertuples(index=False), start=1):
        order_id = row.order_id
        amount = float(row.order_amount) if pd.notna(row.order_amount) else None
        payment = float(row.payment_total) if pd.notna(row.payment_total) else None
        installments = int(row.installments) if pd.notna(row.installments) else 1
        # The real Olist payment total can exceed items + freight: interest on installment payments.
        # Olist has no interest column; PSP statements do, so the bank feed carries it (simulated).
        real_delta = round(payment - amount, 2) if amount is not None and payment is not None else 0.0
        interest = real_delta if installments > 1 and real_delta > DELTA_TOLERANCE else 0.0
        scenario = "MATCHED"
        expected_outcome = "PASS_1"
        expected_exception = ""
        has_payment = True
        has_shipment = row.order_status in {"delivered", "shipped"}
        if amount is None or payment is None:
            scenario = "INCOMPLETE_SOURCE"
            expected_outcome = "EXCEPTION"
            expected_exception = "MISSING_SOURCE_EVIDENCE"
        elif row.order_status not in {"delivered", "shipped"}:
            scenario = "STATUS_PENDING_POLICY"
            expected_outcome = "EXCEPTION"
            expected_exception = "ORDER_STATUS_REQUIRES_POLICY"
        else:
            draw = rng.random()
            if draw < 0.01:
                payment += round(max(1.0, amount * 0.05), 2)
                scenario, expected_outcome, expected_exception = "AMOUNT_ERROR", "EXCEPTION", "AMOUNT_MISMATCH"
            elif draw < 0.015:
                has_payment = False
                scenario, expected_outcome, expected_exception = "MISSING_PAYMENT", "EXCEPTION", "MISSING_PAYMENT"
            elif draw < 0.02:
                has_shipment = False
                scenario, expected_outcome, expected_exception = "MISSING_SHIPMENT", "EXCEPTION", "MISSING_SHIPMENT"
        if scenario == "MATCHED" and abs(real_delta) > DELTA_TOLERANCE:
            if interest:
                scenario, expected_exception = "INSTALLMENT_INTEREST", "INSTALLMENT_INTEREST"
            else:  # a real, unexplained difference in the Olist data: the engine should flag it
                scenario, expected_outcome, expected_exception = "SOURCE_AMOUNT_DELTA", "EXCEPTION", "AMOUNT_MISMATCH"
        tracking = f"OLIST-{order_id}"
        ordered_at = row.order_purchase_timestamp.isoformat()
        delivered_at = row.order_delivered_customer_date
        if pd.isna(delivered_at):
            delivered_at = row.order_estimated_delivery_date
        oms_rows.append({
            "order_id": order_id, "tracking_no": tracking,
            "customer_id": row.customer_id, "payment_method": "PREPAID",
            "currency": "BRL", "order_amount": amount,
            "order_status": row.order_status, "ordered_at": ordered_at,
        })
        if has_payment and payment is not None:
            bank_rows.append({
                "txn_ref": f"OLIST-PAY-{order_id}", "txn_date": ordered_at,
                "value_date": pd.Timestamp(row.order_approved_at).date().isoformat()
                if not pd.isna(row.order_approved_at) else "",
                "description": f"OLIST PAYMENT {order_id}",
                "currency": "BRL", "credit_amount": payment,
                "fee_amount": 0.0, "net_amount": payment,
                "installments": installments, "payment_type": row.payment_type, "interest_amount": interest,
            })
            link_rows.append({"feed": "bank", "record_key": f"OLIST-PAY-{order_id}", "order_id": order_id,
                              "link_kind": "order_ref"})
        if has_shipment:
            logistics_rows.append({
                "tracking_code": tracking, "client_order_code": order_id,
                "status": "DELIVERED" if row.order_status == "delivered" else "SHIPPED",
                "currency": "BRL",
                "cod_collected": 0.0, "ship_fee": float(row.freight_total) if pd.notna(row.freight_total) else None,
                "cod_fee": 0.0, "remittance_id": f"OLIST-SHIP-{order_id}",
                "picked_at": pd.Timestamp(row.order_delivered_carrier_date).isoformat()
                if not pd.isna(row.order_delivered_carrier_date) else "",
                "delivered_at": pd.Timestamp(delivered_at).isoformat()
                if not pd.isna(delivered_at) else "",
            })
            link_rows.append({"feed": "logistics", "record_key": tracking, "order_id": order_id,
                              "link_kind": "order_ref"})
        truth_rows.append({
            "unit_id": unit_id, "scenario": scenario, "sub_kind": scenario,
            "expected_outcome": expected_outcome, "expected_exception": expected_exception,
            "payment_method": "PREPAID", "order_id": order_id,
            "tracking_no": tracking, "bank_txn_ref": f"OLIST-PAY-{order_id}" if has_payment else "",
        })

    out_dir.mkdir(parents=True, exist_ok=True)
    frames = {
        "oms_orders.csv": pd.DataFrame(oms_rows),
        "bank_statement.csv": pd.DataFrame(bank_rows),
        "logistics_report.csv": pd.DataFrame(logistics_rows),
        "ground_truth.csv": pd.DataFrame(truth_rows),
        "links.csv": pd.DataFrame(link_rows, columns=["feed", "record_key", "order_id", "link_kind"]),
    }
    files = {}
    for name, frame in frames.items():
        path = out_dir / name
        frame.to_csv(path, index=False)
        files[name] = {"rows": len(frame), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    manifest = {
        "generator": "recon.olistdata",
        "generator_version": "1.4.0",
        "source": "brazil ecom",
        "source_files": ["olist_orders_dataset.csv", "olist_order_items_dataset.csv", "olist_order_payments_dataset.csv"],
        "seed": seed, "units": len(truth_rows), "currency": "BRL",
        "source_status_counts": orders["order_status"].value_counts().sort_index().to_dict(),
        "scenario_counts": pd.Series([r["scenario"] for r in truth_rows]).value_counts().to_dict(),
        "files": files,
        "simulated_columns": {
            "bank_statement.interest_amount": "real Olist payment total minus items + freight on installment "
                                              "payments that exceed the order; Olist itself has no interest column",
        },
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="brazil ecom")
    parser.add_argument("--out", default="data/generated")
    parser.add_argument("--rows", type=int, default=None, help="optional row cap; default keeps every source order")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    print(generate_from_olist(args.source, args.out, rows=args.rows, seed=args.seed))
