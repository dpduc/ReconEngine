"""Constants, label records, profiles and helpers shared by the whole generator."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd



GENERATOR_VERSION = "2.6.0"
SOURCE_FILES = ("olist_orders_dataset.csv", "olist_order_items_dataset.csv", "olist_order_payments_dataset.csv")
DATE_SHIFT = pd.Timedelta(days=2926)  # 418 weeks: weekdays kept, Black Friday 2017 -> Black Friday 2025
DATE_COLUMNS = ("order_purchase_timestamp", "order_approved_at", "order_delivered_carrier_date",
                "order_delivered_customer_date", "order_estimated_delivery_date")
PRIOR_DAYS = 21  # last month's orders whose money or shipment can still land in this statement
STATEMENT_THROUGH_DAY = 15  # the statement and settlement reports run to this day of the next month
MARKETPLACE_SETTLEMENT_DAYS = 45  # a marketplace order with no line is overdue this long after it was placed
SHIP_DAYS = 7  # a self-shipped order is picked up within this many days of being placed
GRACE_DAYS = 3  # added to a due date before money or a shipment is overdue at the cut-off
CHANNELS = ("website", "shopee", "tiktok", "facebook")
FILES = ("oms_orders.csv", "bank_statement.csv", "logistics_report.csv", "shopee_settlement.csv",
         "tiktok_settlement.csv")
# The answers, kept out of the input folders: `<out>/<market>/labels/<set>/` (see `labels_for`).
LABELS, LABEL_FILES = "labels", ("ground_truth.csv", "links.csv")
DIGITS, ALPHANUMERIC = "0123456789", "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"


@dataclass(frozen=True)
class Truth:
    scenario: str
    outcome: str  # PASS_1 / PASS_2 tie out (AUTO_ACCEPT); REVIEW / EXCEPTION go to the queue
    exception: str = ""


MATCHED_DIRECT, MATCHED_INDIRECT = Truth("MATCHED", "PASS_1"), Truth("MATCHED", "PASS_2")
PENDING = Truth("PENDING_SETTLEMENT", "OPEN", "NOT_YET_DUE")  # money due after the cut-off: an open item
STATUS_PENDING = Truth("STATUS_PENDING_POLICY", "EXCEPTION", "ORDER_STATUS_REQUIRES_POLICY")
MISSING_SHIPMENT = Truth("MISSING_SHIPMENT", "EXCEPTION", "MISSING_SHIPMENT")
MISSING_PAYMENT = Truth("MISSING_PAYMENT", "EXCEPTION", "MISSING_PAYMENT")


@dataclass(frozen=True)
class Profile:
    """One fictional seller: month (after the date shift), size, channel mix and error rates per order."""

    name: str
    description: str
    month: str
    orders: int
    mix: dict[str, float]
    rates: dict[str, float]
    non_sales_credits: int


def load_olist(source_dir: str | Path) -> pd.DataFrame:
    """Olist orders with items and payments, one row each, dates shifted by DATE_SHIFT."""
    source_dir = Path(source_dir)
    orders = pd.read_csv(source_dir / SOURCE_FILES[0], parse_dates=list(DATE_COLUMNS))
    items = pd.read_csv(source_dir / SOURCE_FILES[1]).groupby("order_id", as_index=False).agg(
        item_total=("price", "sum"), freight_total=("freight_value", "sum"))
    payments = pd.read_csv(source_dir / SOURCE_FILES[2])
    totals = payments.groupby("order_id", as_index=False).agg(
        payment_total=("payment_value", "sum"), installments=("payment_installments", "max"))
    primary = (payments.sort_values(["order_id", "payment_value"], ascending=[True, False])
               .drop_duplicates("order_id")[["order_id", "payment_type"]])
    out = orders.merge(items, on="order_id").merge(totals, on="order_id").merge(primary, on="order_id")
    out[list(DATE_COLUMNS)] = out[list(DATE_COLUMNS)] + DATE_SHIFT
    out["item_total"], out["freight_total"] = out["item_total"].round(2), out["freight_total"].round(2)
    out["order_amount"] = (out["item_total"] + out["freight_total"]).round(2)
    return out.sort_values(["order_purchase_timestamp", "order_id"]).reset_index(drop=True)


def _iso(value: Any) -> str:
    return "" if pd.isna(value) else pd.Timestamp(value).isoformat()


def _date(value: Any) -> str:
    return "" if pd.isna(value) else pd.Timestamp(value).date().isoformat()


def _next_weekday(day: pd.Timestamp, weekdays: tuple[int, ...]) -> pd.Timestamp:
    """The first day strictly after `day` falling on one of `weekdays`."""
    day = day.normalize() + pd.Timedelta(days=1)
    while day.weekday() not in weekdays:
        day += pd.Timedelta(days=1)
    return day


def _ascii(text: str) -> str:
    """Upper-case ASCII, as Vietnamese banks print names: 'Nguyễn Thị Lan' -> 'NGUYEN THI LAN'."""
    text = text.replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c)).upper()
