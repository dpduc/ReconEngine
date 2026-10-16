"""Multi-channel test datasets: a small seller's own website plus Shopee, TikTok Shop and Facebook.

Each set is one month of a fictional small seller, built from real Olist orders (order timing,
volume shape, statuses, delivery dates, price spread). What is derived is how the money for each
channel reaches the seller. Two markets share the plumbing:

    vn (default, VND)
        website    cash on delivery, bank transfer with the order code in the note, a wallet gateway
        shopee     escrow released after delivery, paid out weekly in one netted transfer
        tiktok     settled after delivery, paid out twice a week in one netted transfer
        facebook   cash on delivery, or a bank transfer with a free-text note
        Carriers (GHN, GHTK, J&T) remit the COD they collected, less shipping and COD fees, on fixed days.
        Olist prices are re-priced to Vietnamese order values and times after purchase compressed.

    br (BRL)
        website    a payment gateway line per sale: gross, fee by method, net, installments, interest
        shopee / tiktok as above
        facebook   buyer Pix naming only the buyer, a local courier remitting cash on delivery, cash

The OMS holds the month's orders. The bank statement and the settlement reports run to the 15th
of the next month, so they carry payouts for last month's orders and miss money not yet released.
Ground truth is what a correct reconciler with every file concludes, not what the engine does
today; ``links.csv`` names the true order(s) of every bank line, settlement row and shipment.

    python -m recon.channeldata --out test            # test/vn/<set>/
    python -m recon.channeldata --market br --out test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd

from recon.contracts import fee as vnd_fee

GENERATOR_VERSION = "2.3.0"
SOURCE_FILES = ("olist_orders_dataset.csv", "olist_order_items_dataset.csv", "olist_order_payments_dataset.csv")
DATE_SHIFT = pd.Timedelta(days=2926)  # 418 weeks: weekdays kept, Black Friday 2017 -> Black Friday 2025
DATE_COLUMNS = ("order_purchase_timestamp", "order_approved_at", "order_delivered_carrier_date",
                "order_delivered_customer_date", "order_estimated_delivery_date")
PRIOR_DAYS = 21  # last month's orders whose money or shipment can still land in this statement
STATEMENT_THROUGH_DAY = 15  # the statement and settlement reports run to this day of the next month
CHANNELS = ("website", "shopee", "tiktok", "facebook")
FILES = ("oms_orders.csv", "bank_statement.csv", "logistics_report.csv", "shopee_settlement.csv",
         "tiktok_settlement.csv", "ground_truth.csv", "links.csv")
DIGITS, ALPHANUMERIC = "0123456789", "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"


@dataclass(frozen=True)
class Truth:
    scenario: str
    outcome: str  # PASS_1 / PASS_2 tie out (AUTO_ACCEPT); REVIEW / EXCEPTION go to the queue
    exception: str = ""


MATCHED_DIRECT, MATCHED_INDIRECT = Truth("MATCHED", "PASS_1"), Truth("MATCHED", "PASS_2")
PENDING = Truth("PENDING_SETTLEMENT", "REVIEW", "PENDING_SETTLEMENT")
STATUS_PENDING = Truth("STATUS_PENDING_POLICY", "REVIEW", "ORDER_STATUS_REQUIRES_POLICY")
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


# --- assumptions -------------------------------------------------------------------------------

@dataclass(frozen=True)
class VietnamAssumptions:
    """Fees, schedules and re-pricing for the Vietnam market: assumptions for test data, not quoted prices."""

    price_scale: float = 2_500  # VND per Olist BRL of item price: Vietnamese order values, not the FX rate
    freight_scale: float = 1_500  # VND per Olist BRL of freight, clipped to a domestic parcel fee
    freight_min: int = 15_000
    freight_max: int = 60_000
    time_scale: float = 0.3  # times after purchase compressed: Olist delivers in ~12 days, Vietnam in a few
    website_cod: float = 0.50  # website payment routes; the rest after transfer is the wallet gateway
    website_transfer: float = 0.35
    facebook_cod: float = 0.65  # the rest pay by bank transfer
    wallet_mdr_bps: int = 110  # wallet gateway fee on the gross
    cod_fee_bps: int = 80  # carrier fee on COD collected
    return_fee_share: float = 0.5  # a returned parcel is charged this share of its shipping fee again
    cod_payout_weekdays: tuple[int, ...] = (0, 2, 4)  # carriers remit Monday, Wednesday, Friday
    shopee_fixed_rate: float = 0.08  # share of item value
    shopee_payment_rate: float = 0.05
    shopee_release_days: int = 3
    shopee_payout_weekdays: tuple[int, ...] = (1,)  # Tuesday
    tiktok_commission: float = 0.06
    tiktok_transaction_fee: float = 0.04
    tiktok_release_days: int = 7
    tiktok_payout_weekdays: tuple[int, ...] = (0, 3)
    overcharge_points: float = 0.02  # extra fee rate on an overcharged marketplace order


@dataclass(frozen=True)
class BrazilAssumptions:
    """Fees and schedules for the Brazil market: assumptions for test data, not quoted platform prices."""

    card_mdr: float = 0.0399  # gateway fee on card sales, share of gross
    debit_mdr: float = 0.0199
    pix_mdr: float = 0.0099
    boleto_fee: float = 3.49  # flat, per boleto
    boleto_share: float = 0.15  # Olist boleto payments kept as boleto; the rest become Pix
    shopee_commission: float = 0.14
    shopee_fixed_fee: float = 4.00  # per order
    shopee_release_days: int = 3
    shopee_payout_weekdays: tuple[int, ...] = (2,)  # Wednesday
    tiktok_commission: float = 0.06
    tiktok_transaction_fee: float = 0.02
    tiktok_release_days: int = 7
    tiktok_payout_weekdays: tuple[int, ...] = (0, 3)  # Monday, Thursday
    overcharge_points: float = 0.02
    courier_ship_fee: float = 12.00  # local courier, per delivery, charged to the buyer in the order
    courier_cod_fee: float = 0.02  # local courier, share of cash collected
    courier_payout_weekday: int = 4  # Friday


_VN_CALM = {"wallet_missing": 0.005, "wallet_short": 0.005, "wallet_fee_over": 0.005, "transfer_typo": 0.05,
            "transfer_split": 0.02, "transfer_short": 0.01, "transfer_duplicate": 0.005, "transfer_unpaid": 0.005,
            "transfer_other_payer": 0.05, "cod_short": 0.005, "cod_returned": 0.04, "cod_fee_over": 0.005,
            "missing_shipment": 0.005, "market_fee_over": 0.01, "market_return": 0.01}
VN_PROFILES = (
    Profile("a_website_first", "Mostly sells on its own website (COD, transfer, wallet); few errors.", "2026-03",
            600, {"website": 0.70, "shopee": 0.15, "tiktok": 0.10, "facebook": 0.05}, _VN_CALM, 2),
    Profile("b_marketplace_heavy", "Mostly Shopee and TikTok Shop, paid in batched payouts; year-end sale peak.",
            "2025-11", 1500, {"shopee": 0.45, "tiktok": 0.35, "website": 0.15, "facebook": 0.05},
            {**_VN_CALM, "cod_returned": 0.06, "market_fee_over": 0.04, "market_return": 0.03}, 3),
    Profile("c_social_heavy", "Facebook first: COD with frequent returns, transfers with free-text notes.",
            "2026-06", 400, {"facebook": 0.55, "website": 0.20, "shopee": 0.15, "tiktok": 0.10},
            {**_VN_CALM, "transfer_typo": 0.10, "transfer_split": 0.10, "transfer_short": 0.05,
             "transfer_duplicate": 0.03, "transfer_unpaid": 0.03, "transfer_other_payer": 0.12, "cod_short": 0.02,
             "cod_returned": 0.12, "cod_fee_over": 0.01, "missing_shipment": 0.02, "market_fee_over": 0.02,
             "market_return": 0.02}, 5),
)
_BR_CALM = {"fb_cod": 0.10, "fb_cash": 0.05, "fb_split": 0.05, "fb_haggle": 0.03, "fb_duplicate": 0.01,
            "fb_unpaid": 0.01, "fb_other_payer": 0.05, "cod_short": 0.02, "fb_manual_label": 0.20}
BR_PROFILES = (
    Profile("a_website_first", "Mostly sells on its own website; few errors.", "2026-03", 600,
            {"website": 0.70, "shopee": 0.15, "tiktok": 0.10, "facebook": 0.05},
            {"gateway_missing": 0.005, "gateway_short": 0.005, "gateway_fee_over": 0.005, "missing_shipment": 0.005,
             "market_fee_over": 0.01, "market_return": 0.01, **_BR_CALM}, 2),
    Profile("b_marketplace_heavy", "Mostly Shopee and TikTok Shop, paid in batched payouts; Black Friday month.",
            "2025-11", 1500, {"shopee": 0.45, "tiktok": 0.30, "website": 0.20, "facebook": 0.05},
            {"gateway_missing": 0.01, "gateway_short": 0.01, "gateway_fee_over": 0.01, "missing_shipment": 0.01,
             "market_fee_over": 0.04, "market_return": 0.03, **_BR_CALM}, 3),
    Profile("c_social_heavy", "Facebook Marketplace first: Pix by buyer name, a local COD courier, cash at pickup.",
            "2026-06", 400, {"facebook": 0.50, "website": 0.25, "shopee": 0.15, "tiktok": 0.10},
            {"gateway_missing": 0.01, "gateway_short": 0.01, "gateway_fee_over": 0.01, "missing_shipment": 0.02,
             "market_fee_over": 0.02, "market_return": 0.02, "fb_cod": 0.25, "fb_cash": 0.10, "fb_split": 0.08,
             "fb_haggle": 0.08, "fb_duplicate": 0.03, "fb_unpaid": 0.04, "fb_other_payer": 0.10, "cod_short": 0.06,
             "fb_manual_label": 0.35}, 5),
)


# --- source ------------------------------------------------------------------------------------

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


def localize_vn(olist: pd.DataFrame, a: VietnamAssumptions = VietnamAssumptions()) -> pd.DataFrame:
    """Olist re-priced to VND (rounded to 1,000) with times after purchase compressed by `time_scale`."""
    out = olist.copy()
    out["item_total"] = ((out["item_total"] * a.price_scale / 1_000).round() * 1_000).clip(lower=1_000).astype(int)
    freight = (out["freight_total"] * a.freight_scale).clip(a.freight_min, a.freight_max)
    out["freight_total"] = ((freight / 1_000).round() * 1_000).astype(int)
    out["order_amount"] = out["item_total"] + out["freight_total"]
    out["payment_total"] = out["order_amount"]
    placed = out["order_purchase_timestamp"]
    for column in DATE_COLUMNS[1:]:
        out[column] = (placed + (out[column] - placed) * a.time_scale).dt.floor("s")
    return out


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


# --- shared plumbing ---------------------------------------------------------------------------

@dataclass
class _Seller:
    """Collects one set's rows while orders are replayed; batched payouts are flushed at the end."""

    profile: Profile
    rng: random.Random
    start: pd.Timestamp
    end: pd.Timestamp  # exclusive end of the statement
    a: Any = None
    rows: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    batches: dict[tuple[str, str, pd.Timestamp], list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list))
    seq: int = 0
    used: set[str] = field(default_factory=set)

    CURRENCY: ClassVar[str] = ""
    # settlement file -> (order number, payout id, net) columns
    SETTLEMENT_KEYS: ClassVar[dict[str, tuple[str, str, str]]] = {}

    def money(self, value: float) -> float | int:
        raise NotImplementedError

    def name(self) -> str:
        raise NotImplementedError

    # --- identifiers -------------------------------------------------------------------------

    def _unique(self, make) -> str:
        while (value := make()) in self.used:
            pass
        self.used.add(value)
        return value

    def _code(self, n: int, alphabet: str = ALPHANUMERIC) -> str:
        return "".join(self.rng.choice(alphabet) for _ in range(n))

    def _txn_ref(self, kind: str, when: pd.Timestamp) -> str:
        self.seq += 1
        return f"{kind.upper()}{when:%Y%m%d}{self.seq:06d}"

    # --- shared records ----------------------------------------------------------------------

    def _in_window(self, when: Any) -> bool:
        return when is not None and not pd.isna(when) and self.start <= pd.Timestamp(when) < self.end

    def _link(self, feed: str, key: str, order_id: str, kind: str, note: str = "") -> None:
        self.rows["links"].append({"feed": feed, "record_key": key, "order_id": order_id,
                                   "link_kind": kind, "note": note})

    def _bank(self, when: pd.Timestamp, description: str, credit: float, *, kind: str, fee: float = 0,
              installments: Any = "", payment_type: str = "", interest: float = 0,
              value_date: pd.Timestamp | None = None) -> str | None:
        """Add a credit when it lands in the statement; returns its txn_ref (None outside the window)."""
        value_date = when.normalize() if value_date is None else value_date
        if not self._in_window(value_date):
            return None
        ref = self._txn_ref(kind, when)
        self.rows["bank"].append({
            "txn_ref": ref, "txn_date": _iso(when), "value_date": _date(value_date), "description": description,
            "currency": self.CURRENCY, "credit_amount": self.money(credit), "fee_amount": self.money(fee),
            "net_amount": self.money(credit - fee), "installments": installments, "payment_type": payment_type,
            "interest_amount": self.money(interest)})
        return ref

    def _shipment(self, order_id: str, tracking: str, carrier: str, service: str, *, labelled: bool,
                  current: bool, picked: pd.Timestamp, delivered: Any, ship_fee: float, cod: float = 0,
                  cod_fee: float = 0, remittance: str = "", remitted: bool = False, status: str = "") -> None:
        """Add a shipment picked in the window, or one on a remittance paid in it (`remitted`)."""
        if not (self._in_window(picked) or remitted):
            return
        self.rows["logistics"].append({
            "tracking_code": tracking, "client_order_code": order_id if labelled else "", "carrier": carrier,
            "service": service, "status": status or ("DELIVERED" if not pd.isna(delivered) else "SHIPPED"),
            "currency": self.CURRENCY, "cod_collected": self.money(cod), "ship_fee": self.money(ship_fee),
            "cod_fee": self.money(cod_fee), "remittance_id": remittance, "picked_at": _iso(picked),
            "delivered_at": _iso(delivered)})
        self._link("logistics", tracking, order_id, "order_ref" if labelled else "tracking",
                   "" if current else "PRIOR_PERIOD")

    @staticmethod
    def _dates(o: Any) -> tuple[pd.Timestamp, Any]:
        """(picked, delivered) for a self-shipped order: carrier date (or a day after approval), delivery date."""
        paid = o.order_approved_at if not pd.isna(o.order_approved_at) else o.order_purchase_timestamp
        picked = o.order_delivered_carrier_date if not pd.isna(o.order_delivered_carrier_date) \
            else paid + pd.Timedelta(days=1)
        delivered = o.order_delivered_customer_date if o.order_status == "delivered" else pd.NaT
        return picked, delivered

    def _pick(self, choices: list[tuple[str, float]]) -> str | None:
        """One of `choices` by its probability, or None for the remainder."""
        draw, total = self.rng.random(), 0.0
        for name, probability in choices:
            total += probability
            if draw < total:
                return name
        return None

    # --- marketplaces (same shape in both markets) -------------------------------------------

    def platform_terms(self, platform: str, o: Any) -> tuple[str, str, float, float, int, tuple[int, ...]]:
        """(order number, tracking, commission rate, other fees, release days, payout weekdays)."""
        raise NotImplementedError

    def marketplace(self, o: Any, current: bool, platform: str) -> tuple[dict[str, Any], Truth, str]:
        rates = self.profile.rates
        order_no, tracking, commission, fees, release_days, weekdays = self.platform_terms(platform, o)
        oms = {"channel_order_no": order_no, "tracking_no": tracking, "payment_method": "PREPAID",
               "payment_type": platform, "installments": "", "order_amount": o.order_amount}
        if o.order_status != "delivered":
            return oms, PENDING if o.order_status == "shipped" else STATUS_PENDING, ""
        truth, refund = MATCHED_INDIRECT, 0
        if current:
            injected = self._pick([("fee", rates["market_fee_over"]), ("return", rates["market_return"])])
            if injected == "fee":
                commission += self.a.overcharge_points
                truth = Truth("FEE_OVERCHARGE", "EXCEPTION", "FEE_MISMATCH")
            elif injected == "return":
                truth = Truth("RETURN_NOT_IN_OMS", "EXCEPTION", "AMOUNT_MISMATCH")
        commission_fee = self.money(o.item_total * commission)
        if truth.scenario == "RETURN_NOT_IN_OMS":  # refunded to the buyer; the platform waives its fees
            commission_fee, fees, refund = 0, 0, o.item_total
        released = o.order_delivered_customer_date + pd.Timedelta(days=release_days)
        payout_day = _next_weekday(released, weekdays)
        if not self._in_window(payout_day):
            return oms, PENDING if current else truth, ""
        self.batches[(platform, platform, payout_day)].append({
            "order_id": o.order_id, "order_no": order_no, "created": o.order_purchase_timestamp,
            "released": released, "gross": o.item_total, "freight": o.freight_total, "commission": commission_fee,
            "fees": fees, "refund": refund, "net": self.money(o.item_total - commission_fee - fees - refund),
            "current": current})
        return oms, truth, ""

    def tiktok_row(self, row: dict[str, Any], payout_id: str) -> dict[str, Any]:
        return {"Order ID": row["order_no"], "Statement ID": payout_id,
                "Order created time": f"{row['created']:%Y/%m/%d %H:%M:%S}",
                "Order settled time": f"{row['released']:%Y/%m/%d %H:%M:%S}",
                "Total revenue": row["gross"], "Shipping fee paid by buyer": row["freight"],
                "Actual shipping fee": -row["freight"] or 0,  # passed to the carrier; nets to zero
                "Platform commission": -row["commission"] or 0, "Transaction fee": -row["fees"] or 0,
                "Refund": -row["refund"] or 0, "Total settlement amount": row["net"]}

    def shopee_row(self, row: dict[str, Any], payout_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def payout_line(self, kind: str, label: str, payout_id: str) -> tuple[str, str]:
        """(bank description, txn kind) of a batched payout."""
        raise NotImplementedError

    def flush_payouts(self) -> None:
        for (kind, label, day), rows in sorted(self.batches.items(), key=lambda item: (item[0][2], item[0][:2])):
            credit = self.money(sum(row["net"] for row in rows))
            payout_id = f"{day:%Y%m%d}{self._code(6, DIGITS)}" if kind != "cod" else rows[0]["remittance"]
            description, txn_kind = self.payout_line(kind, label, payout_id)
            ref = self._bank(day + pd.Timedelta(hours=10 if kind != "cod" else 16), description, credit,
                             kind=txn_kind) if credit > 0 else None
            for row in rows:
                note = "" if row["current"] else "PRIOR_PERIOD"
                if ref:
                    self._link("bank", ref, row["order_id"], "cod_remittance" if kind == "cod" else "payout_batch",
                               note)
                if kind in ("shopee", "tiktok"):
                    self._link(kind, row["order_no"], row["order_id"], "order_no", note)
                    self.rows[kind].append((self.shopee_row if kind == "shopee" else self.tiktok_row)(row, payout_id))

    def non_sales_lines(self) -> list[tuple[str, float, str]]:
        """(description, credit, txn kind) of credits that are not sales, cycled through."""
        raise NotImplementedError

    def non_sales(self) -> None:
        lines, span = self.non_sales_lines(), (self.end - self.start).days
        for number in range(self.profile.non_sales_credits):
            when = self.start + pd.Timedelta(days=self.rng.randrange(span), hours=self.rng.randint(8, 20))
            description, credit, kind = lines[number % len(lines)]
            ref = self._bank(when, description, credit, kind=kind)
            self._link("bank", ref, "", "none", "NOT_A_SALE")


# --- Vietnam -----------------------------------------------------------------------------------

VN_FAMILY = ("Nguyễn", "Trần", "Lê", "Phạm", "Hoàng", "Huỳnh", "Phan", "Vũ", "Võ", "Đặng", "Bùi", "Đỗ", "Hồ", "Ngô",
             "Dương", "Lý")
VN_MIDDLE = ("Văn", "Thị", "Hữu", "Minh", "Ngọc", "Thanh", "Quốc", "Đức", "Thu", "Gia", "Hoài", "Bảo")
VN_GIVEN = ("An", "Bình", "Châu", "Dũng", "Giang", "Hà", "Hải", "Hạnh", "Hoa", "Hùng", "Hương", "Khánh", "Lan",
            "Linh", "Long", "Mai", "Nam", "Ngân", "Nhung", "Phong", "Phương", "Quân", "Tâm", "Thảo", "Trang",
            "Tuấn", "Vy", "Yến", "Đạt", "Trinh")
VN_BANKS = ("MBVCB", "TCB", "ACB", "VPB", "BIDV", "MB", "TPB", "VIB")
VN_CARRIERS = ("GHN", "GHTK", "J&T")


@dataclass
class _VietnamSeller(_Seller):
    CURRENCY = "VND"
    SETTLEMENT_KEYS = {"shopee_settlement.csv": ("Mã đơn hàng", "Mã thanh toán", "Tổng tiền đã thanh toán"),
                       "tiktok_settlement.csv": ("Order ID", "Statement ID", "Total settlement amount")}

    def money(self, value: float) -> int:
        return int(round(value))

    def name(self) -> str:
        return f"{self.rng.choice(VN_FAMILY)} {self.rng.choice(VN_MIDDLE)} {self.rng.choice(VN_GIVEN)}"

    def _txn_ref(self, kind: str, when: pd.Timestamp) -> str:  # bank reference, e.g. FT26061000001234
        self.seq += 1
        return f"FT{when:%y%m%d}{self.seq:08d}"

    def _tracking(self, carrier: str) -> str:
        return self._unique(lambda: {"GHN": lambda: "G" + self._code(7), "GHTK": lambda: "S" + self._code(9, DIGITS),
                                     "J&T": lambda: "8" + self._code(11, DIGITS)}[carrier]())

    def _carrier_shipment(self, o: Any, current: bool, *, cod: int, truth: Truth,
                          carrier: str, tracking: str) -> Truth:
        """Ship through a carrier; COD (if any) comes back on its next remittance, less shipping and COD fees."""
        a = self.a
        picked, delivered = self._dates(o)
        returned = truth.scenario == "COD_RETURNED_NOT_IN_OMS"
        collected = 0 if returned or pd.isna(delivered) else cod
        if truth.scenario == "COD_SHORT":
            collected = cod - min(self.rng.choice((20_000, 50_000, 100_000)), cod // 2 // 1_000 * 1_000)
        ship_fee = o.freight_total + (self.money(o.freight_total * a.return_fee_share / 1_000) * 1_000
                                      if returned else 0)
        cod_fee = vnd_fee(collected, a.cod_fee_bps) + (5_000 if truth.scenario == "COD_FEE_OVERCHARGE" else 0)
        settled = picked + pd.Timedelta(days=self.rng.randint(3, 6)) if returned else delivered
        if current and truth == MATCHED_INDIRECT and not pd.isna(delivered) and picked > delivered:
            truth = Truth("SOURCE_TIMING_ANOMALY", "EXCEPTION", "TIMING_MISMATCH")
        payout_day = None if pd.isna(settled) else _next_weekday(settled, a.cod_payout_weekdays)
        remittance = f"{carrier.replace('&', '')}-{payout_day:%y%m%d}" if payout_day is not None else ""
        remitted = self._in_window(payout_day)
        self._shipment(o.order_id, tracking, carrier, "COD" if cod else "Standard", labelled=True, current=current,
                       picked=picked, delivered=pd.NaT if returned else delivered, ship_fee=ship_fee,
                       cod=collected, cod_fee=cod_fee, remittance=remittance, remitted=remitted,
                       status="RETURNED" if returned else "")
        if remitted:
            self.batches[("cod", carrier, payout_day)].append({
                "order_id": o.order_id, "net": collected - ship_fee - cod_fee, "current": current,
                "remittance": remittance})
        elif cod and current and truth.outcome != "EXCEPTION":
            truth = PENDING
        return truth

    def _cod(self, o: Any, current: bool) -> tuple[dict[str, Any], Truth, str]:
        rates = self.profile.rates
        carrier = self.rng.choice(VN_CARRIERS)
        tracking = self._tracking(carrier)
        oms = {"channel_order_no": "", "tracking_no": tracking, "payment_method": "COD", "payment_type": "cod",
               "installments": "", "order_amount": o.order_amount}
        if o.order_status not in ("delivered", "shipped"):
            return oms, STATUS_PENDING, ""
        truth = MATCHED_INDIRECT
        if current:
            injected = self._pick([("short", rates["cod_short"]), ("returned", rates["cod_returned"]),
                                   ("fee", rates["cod_fee_over"]), ("ship", rates["missing_shipment"])])
            if injected == "ship":
                return oms, MISSING_SHIPMENT, ""
            truth = {"short": Truth("COD_SHORT", "EXCEPTION", "AMOUNT_MISMATCH"),
                     "returned": Truth("COD_RETURNED_NOT_IN_OMS", "EXCEPTION", "MISSING_PAYMENT"),
                     "fee": Truth("COD_FEE_OVERCHARGE", "EXCEPTION", "COD_FEE_MISMATCH")}.get(injected, truth)
        return oms, self._carrier_shipment(o, current, cod=o.order_amount, truth=truth, carrier=carrier,
                                           tracking=tracking), ""

    def _transfer(self, o: Any, current: bool, buyer: str, *, with_code: bool) -> tuple[dict[str, Any], Truth, str]:
        """Prepaid by bank transfer; the note carries the order code on the website, free text on Facebook."""
        rates = self.profile.rates
        carrier = self.rng.choice(VN_CARRIERS)
        tracking = self._tracking(carrier)
        amount = o.order_amount
        oms = {"channel_order_no": "", "tracking_no": tracking, "payment_method": "PREPAID",
               "payment_type": "bank_transfer", "installments": "", "order_amount": amount}
        if o.order_status in ("canceled", "unavailable"):
            return oms, STATUS_PENDING, ""  # cancelled before the buyer paid
        live = o.order_status in ("delivered", "shipped")
        payer = buyer
        note = o.order_id if with_code else self.rng.choice(
            ("chuyen tien", "ck mua hang", f"0{self._code(9, DIGITS)}", "thanh toan don", ""))
        truth = MATCHED_DIRECT if with_code else MATCHED_INDIRECT
        first = o.order_purchase_timestamp + pd.Timedelta(minutes=self.rng.randint(2, 240))
        payments, shipped = [(first, amount, note)], live
        if current:
            injected = self._pick([("typo", rates["transfer_typo"] if with_code else 0.0),
                                   ("split", rates["transfer_split"]), ("short", rates["transfer_short"]),
                                   ("duplicate", rates["transfer_duplicate"]), ("unpaid", rates["transfer_unpaid"]),
                                   ("other", 0.0 if with_code else rates["transfer_other_payer"]),
                                   ("ship", rates["missing_shipment"])])
            if injected == "typo":  # the code no longer parses as an order reference
                code = o.order_id
                note = self.rng.choice((code.replace("-", ""), f"DH {code[-4:]}", code.replace("ORD", "0RD")))
                payments, truth = [(first, amount, note)], Truth("NOTE_TYPO", "PASS_2")
            elif injected == "split":  # a deposit (coc) first, the rest before shipping
                deposit = int(round(amount * self.rng.uniform(0.3, 0.5), -4))
                payments = [(first, deposit, f"coc {note}".strip()),
                            (first + pd.Timedelta(hours=self.rng.randint(20, 48)), amount - deposit, note)]
                truth = Truth("SPLIT_PAYMENT", "PASS_2")
            elif injected == "short":
                payments = [(first, amount - min(self.rng.choice((10_000, 20_000, 50_000)), amount // 4), note)]
                truth = Truth("SHORT_TRANSFER", "EXCEPTION", "AMOUNT_MISMATCH")
            elif injected == "duplicate":
                payments = [(first, amount, note), (first + pd.Timedelta(minutes=self.rng.randint(1, 10)), amount,
                                                    note)]
                truth = Truth("DUPLICATE_PAYMENT", "EXCEPTION", "DUPLICATE_PAYMENT")
            elif injected == "unpaid":
                payments, truth = [], MISSING_PAYMENT
            elif injected == "other":  # a relative pays; neither name nor note points at the order
                payer, truth = self.name(), Truth("PAYER_NAME_DIFFERS", "REVIEW", "LINK_CANDIDATES")
            elif injected == "ship":
                shipped, truth = False, MISSING_SHIPMENT
        if not live and truth.outcome.startswith("PASS"):
            truth = STATUS_PENDING
        first_ref, kind = "", "order_ref" if truth == MATCHED_DIRECT else "name_amount"
        for number, (paid_at, value, text) in enumerate(payments):
            description = f"{self.rng.choice(VN_BANKS)}.{self._code(10, DIGITS)}.{_ascii(payer)} chuyen tien {text}"
            ref = self._bank(paid_at, description.strip(), value, kind="ft", payment_type="bank_transfer")
            if ref:
                link_kind = "split" if truth.scenario == "SPLIT_PAYMENT" else kind
                dup = "DUPLICATE" if truth.scenario == "DUPLICATE_PAYMENT" and number else ""
                self._link("bank", ref, o.order_id, link_kind, dup or ("" if current else "PRIOR_PERIOD"))
                first_ref = first_ref or ref
        if shipped:
            truth = self._carrier_shipment(o, current, cod=0, truth=truth, carrier=carrier, tracking=tracking)
        return oms, truth, first_ref

    def _wallet(self, o: Any, current: bool) -> tuple[dict[str, Any], Truth, str]:
        """Paid through the wallet gateway: one line per sale naming the order, gross less the gateway fee."""
        a, rates = self.a, self.profile.rates
        carrier = self.rng.choice(VN_CARRIERS)
        tracking = self._tracking(carrier)
        credit = o.order_amount
        fee = vnd_fee(credit, a.wallet_mdr_bps)
        oms = {"channel_order_no": "", "tracking_no": tracking, "payment_method": "PREPAID", "payment_type": "wallet",
               "installments": "", "order_amount": credit}
        paid, shipped, truth = True, o.order_status in ("delivered", "shipped"), MATCHED_DIRECT
        if o.order_status in ("canceled", "unavailable"):
            return oms, STATUS_PENDING, ""  # the gateway voids an unshipped cancelled sale
        if not shipped:
            truth = STATUS_PENDING
        elif current:
            injected = self._pick([("missing", rates["wallet_missing"]), ("short", rates["wallet_short"]),
                                   ("fee", rates["wallet_fee_over"]), ("ship", rates["missing_shipment"])])
            if injected == "missing":
                paid, truth = False, MISSING_PAYMENT
            elif injected == "short":
                credit -= max(10_000, credit // 20 // 1_000 * 1_000)
                truth = Truth("CREDIT_SHORT", "EXCEPTION", "AMOUNT_MISMATCH")
            elif injected == "fee":
                fee += max(2_000, vnd_fee(credit, 100))
                truth = Truth("FEE_OVERCHARGE", "EXCEPTION", "FEE_MISMATCH")
            elif injected == "ship":
                shipped, truth = False, MISSING_SHIPMENT
        ref = None
        paid_at = o.order_approved_at if not pd.isna(o.order_approved_at) else o.order_purchase_timestamp
        if paid:
            ref = self._bank(paid_at, f"VI DIEN TU TT DON {o.order_id}", credit, kind="ft", fee=fee,
                             payment_type="wallet", value_date=paid_at.normalize() + pd.Timedelta(days=1))
            if ref:
                self._link("bank", ref, o.order_id, "order_ref", "" if current else "PRIOR_PERIOD")
        if shipped:
            truth = self._carrier_shipment(o, current, cod=0, truth=truth, carrier=carrier, tracking=tracking)
        return oms, truth, ref or ""

    def website(self, o: Any, current: bool, buyer: str) -> tuple[dict[str, Any], Truth, str]:
        route = self._pick([("cod", self.a.website_cod), ("transfer", self.a.website_transfer)]) or "wallet"
        if route == "cod":
            return self._cod(o, current)
        if route == "transfer":
            return self._transfer(o, current, buyer, with_code=True)
        return self._wallet(o, current)

    def facebook(self, o: Any, current: bool, buyer: str) -> tuple[dict[str, Any], Truth, str]:
        if self.rng.random() < self.a.facebook_cod:
            return self._cod(o, current)
        return self._transfer(o, current, buyer, with_code=False)

    def platform_terms(self, platform: str, o: Any) -> tuple[str, str, float, float, int, tuple[int, ...]]:
        a = self.a
        if platform == "shopee":
            return (self._unique(lambda: f"{o.order_purchase_timestamp:%y%m%d}{self._code(8)}"),
                    self._unique(lambda: f"SPXVN0{self._code(11, DIGITS)}"), a.shopee_fixed_rate,
                    self.money(o.item_total * a.shopee_payment_rate), a.shopee_release_days, a.shopee_payout_weekdays)
        return (self._unique(lambda: "57" + self._code(16, DIGITS)), self._unique(lambda: f"TTVN{self._code(12, DIGITS)}"),
                a.tiktok_commission, self.money(o.item_total * a.tiktok_transaction_fee), a.tiktok_release_days,
                a.tiktok_payout_weekdays)

    def shopee_row(self, row: dict[str, Any], payout_id: str) -> dict[str, Any]:
        return {"Mã đơn hàng": row["order_no"], "Mã thanh toán": payout_id,
                "Ngày đặt hàng": f"{row['created']:%d/%m/%Y %H:%M}",
                "Ngày hoàn thành thanh toán": f"{row['released']:%d/%m/%Y}", "Giá sản phẩm": row["gross"],
                "Phí cố định": row["commission"], "Phí thanh toán": row["fees"], "Hoàn tiền": row["refund"],
                "Tổng tiền đã thanh toán": row["net"]}

    def payout_line(self, kind: str, label: str, payout_id: str) -> tuple[str, str]:
        if kind == "cod":
            return f"{label} CHUYEN TIEN COD PHIEN {payout_id}", "ft"
        name = "SHOPEE" if kind == "shopee" else "TIKTOK SHOP"
        return f"{name} THANH TOAN DOANH THU {payout_id}", "ft"

    def non_sales_lines(self) -> list[tuple[str, float, str]]:
        return [(f"NOP TIEN MAT - {_ascii(self.name())}", float(self.rng.choice((5, 10, 20)) * 1_000_000), "ft"),
                ("TRA LAI TIEN GUI KHONG KY HAN", float(self.rng.randint(5, 80) * 1_000), "ft"),
                (f"{self.rng.choice(VN_BANKS)}.{self._code(10, DIGITS)}.CONG TY TNHH {_ascii(self.name())} "
                 "hoan tien hang loi", float(self.rng.randint(20, 300) * 10_000), "ft")]


# --- Brazil ------------------------------------------------------------------------------------

BR_FIRST = ("Ana", "Bruno", "Camila", "Daniel", "Eduarda", "Felipe", "Gabriela", "Gustavo", "Helena", "Igor",
            "Juliana", "Lucas", "Larissa", "Marcos", "Mariana", "Natalia", "Otavio", "Paula", "Rafael", "Renata",
            "Sofia", "Thiago", "Vanessa", "Vinicius", "Beatriz", "Carlos", "Fernanda", "Joao", "Leticia", "Pedro")
BR_LAST = ("Silva", "Santos", "Oliveira", "Souza", "Rodrigues", "Ferreira", "Alves", "Pereira", "Lima", "Gomes",
           "Costa", "Ribeiro", "Martins", "Carvalho", "Almeida", "Lopes", "Soares", "Fernandes", "Vieira", "Barbosa")
BR_TOLERANCE = 0.01 + 1e-9


def _s10(rng: random.Random, prefix: str) -> str:
    """A Correios-style tracking code: 2 letters, 8 digits, the S10 check digit, BR."""
    digits = [rng.randrange(10) for _ in range(8)]
    remainder = 11 - sum(d * w for d, w in zip(digits, (8, 6, 4, 2, 3, 5, 9, 7))) % 11
    check = {10: 0, 11: 5}.get(remainder, remainder)
    return f"{prefix}{''.join(map(str, digits))}{check}BR"


@dataclass
class _BrazilSeller(_Seller):
    CURRENCY = "BRL"
    SETTLEMENT_KEYS = {"shopee_settlement.csv": ("ID do pedido", "ID do repasse", "Valor liberado"),
                       "tiktok_settlement.csv": ("Order ID", "Statement ID", "Total settlement amount")}

    def money(self, value: float) -> float:
        return round(value, 2)

    def name(self) -> str:
        return f"{self.rng.choice(BR_FIRST)} {self.rng.choice(BR_LAST)} {self.rng.choice(BR_LAST)}"

    def _txn_ref(self, kind: str, when: pd.Timestamp) -> str:
        if kind == "pix":  # end-to-end id: E + payer bank ISPB + yyyymmddHHMM + 11 characters
            return self._unique(lambda: f"E{self._code(8, DIGITS)}{when:%Y%m%d%H%M}{self._code(11)}")
        return super()._txn_ref(kind, when)

    def website(self, o: Any, current: bool, buyer: str) -> tuple[dict[str, Any], Truth, str]:
        a, rates = self.a, self.profile.rates
        method = o.payment_type
        if method in ("boleto", "voucher", "not_defined"):
            method = "boleto" if method == "boleto" and self.rng.random() < a.boleto_share else "pix"
        credit = float(o.payment_total)
        delta = round(credit - o.order_amount, 2)
        interest = delta if method == "credit_card" and o.installments > 1 and delta > BR_TOLERANCE else 0.0
        fee = a.boleto_fee if method == "boleto" else credit * {
            "credit_card": a.card_mdr, "debit_card": a.debit_mdr, "pix": a.pix_mdr}[method]
        correios = self.rng.random() < 0.7
        tracking = _s10(self.rng, self.rng.choice(("QB", "OB"))) if correios else self._unique(
            lambda: self._code(14, DIGITS))
        paid, shipped, refunded = True, o.order_status in ("delivered", "shipped"), False
        truth = MATCHED_DIRECT
        if not shipped:
            refunded = o.order_status in ("canceled", "unavailable")
            truth = Truth("CANCELED_REFUNDED", "REVIEW", "ORDER_STATUS_REQUIRES_POLICY") if refunded else STATUS_PENDING
        elif current:
            injected = self._pick([("missing", rates["gateway_missing"]), ("short", rates["gateway_short"]),
                                   ("fee", rates["gateway_fee_over"]), ("ship", rates["missing_shipment"])])
            if injected == "missing":
                paid, truth = False, MISSING_PAYMENT
            elif injected == "short":
                credit -= max(1.0, round(credit * 0.05, 2))
                truth = Truth("CREDIT_SHORT", "EXCEPTION", "AMOUNT_MISMATCH")
            elif injected == "fee":
                fee += round(credit * 0.01, 2)
                truth = Truth("FEE_OVERCHARGE", "EXCEPTION", "FEE_MISMATCH")
            elif injected == "ship":
                shipped, truth = False, MISSING_SHIPMENT
            elif interest:
                truth = Truth("INSTALLMENT_INTEREST", "PASS_1", "INSTALLMENT_INTEREST")
            elif abs(delta) > BR_TOLERANCE:  # a real, unexplained difference in the Olist data
                truth = Truth("SOURCE_AMOUNT_DELTA", "EXCEPTION", "AMOUNT_MISMATCH")
        note = "" if current else "PRIOR_PERIOD"
        paid_at = o.order_approved_at if not pd.isna(o.order_approved_at) else o.order_purchase_timestamp
        lag = pd.Timedelta(days=0 if method == "pix" else 1)
        ref = None
        if paid:
            ref = self._bank(paid_at, f"GATEWAY VENDA {o.order_id}", credit, kind="gw", fee=round(fee, 2),
                             installments=int(o.installments), payment_type=method, interest=interest,
                             value_date=paid_at.normalize() + lag)
            if ref:
                self._link("bank", ref, o.order_id, "order_ref", note)
        if refunded and paid:
            when = paid_at + pd.Timedelta(days=self.rng.randint(2, 6))
            back = self._bank(when, f"GATEWAY ESTORNO {o.order_id}", -credit, kind="gw", payment_type=method)
            if back:
                self._link("bank", back, o.order_id, "order_ref", "REFUND" if current else "PRIOR_PERIOD")
        if shipped:
            picked, delivered = self._dates(o)
            if current and truth == MATCHED_DIRECT and not pd.isna(delivered) and picked > delivered:
                truth = Truth("SOURCE_TIMING_ANOMALY", "EXCEPTION", "TIMING_MISMATCH")
            self._shipment(o.order_id, tracking, "Correios" if correios else "Jadlog",
                           self.rng.choice(("PAC", "SEDEX")) if correios else ".Package", labelled=True,
                           current=current, picked=picked, delivered=delivered, ship_fee=o.freight_total)
        oms = {"channel_order_no": "", "tracking_no": tracking, "payment_method": "PREPAID", "payment_type": method,
               "installments": int(o.installments), "order_amount": o.order_amount}
        return oms, truth, ref or ""

    def facebook(self, o: Any, current: bool, buyer: str) -> tuple[dict[str, Any], Truth, str]:
        a, rates = self.a, self.profile.rates
        when, amount = o.order_purchase_timestamp, float(o.order_amount)
        mode = self._pick([("cod", rates["fb_cod"]), ("cash", rates["fb_cash"])]) or "pix"
        note = "" if current else "PRIOR_PERIOD"
        live = o.order_status in ("delivered", "shipped")
        if mode == "cash":
            oms = {"channel_order_no": "", "tracking_no": "", "payment_method": "CASH", "payment_type": "cash",
                   "installments": "", "order_amount": amount}
            return oms, Truth("CASH_AT_PICKUP", "REVIEW", "CASH_UNVERIFIED") if live else STATUS_PENDING, ""
        if mode == "cod":
            amount = round(o.item_total + a.courier_ship_fee, 2)
            tracking = self._unique(lambda: f"RAP{self._code(9, DIGITS)}")
            oms = {"channel_order_no": "", "tracking_no": tracking, "payment_method": "COD", "payment_type": "cash",
                   "installments": "", "order_amount": amount}
            if not live:
                return oms, STATUS_PENDING, ""
            picked = when.normalize() + pd.Timedelta(days=1, hours=self.rng.randint(9, 17))
            delivered = picked + pd.Timedelta(hours=self.rng.randint(2, 50)) if o.order_status == "delivered" \
                else pd.NaT
            collected, truth = amount, MATCHED_INDIRECT
            if current and self.rng.random() < rates["cod_short"]:
                collected = round(amount - min(self.rng.choice((10, 20, 50)), amount / 2), 2)
                truth = Truth("COD_SHORT", "EXCEPTION", "AMOUNT_MISMATCH")
            cod_fee = round(collected * a.courier_cod_fee, 2) if not pd.isna(delivered) else 0.0
            payout_day = _next_weekday(delivered, (a.courier_payout_weekday,)) if not pd.isna(delivered) else None
            remittance = f"RAP-REM-{payout_day:%Y%m%d}" if payout_day is not None else ""
            remitted = self._in_window(payout_day)
            self._shipment(o.order_id, tracking, "Rapida Entregas", "Motoboy", labelled=True, current=current,
                           picked=picked, delivered=delivered, ship_fee=a.courier_ship_fee,
                           cod=collected if remittance else 0.0, cod_fee=cod_fee, remittance=remittance,
                           remitted=remitted)
            if not remitted:
                return oms, PENDING if current else truth, ""
            self.batches[("cod", "RAPIDA ENTREGAS LTDA", payout_day)].append({
                "order_id": o.order_id, "net": round(collected - a.courier_ship_fee - cod_fee, 2),
                "current": current, "remittance": remittance})
            return oms, truth, ""

        tracking = _s10(self.rng, self.rng.choice(("QB", "OB")))
        oms = {"channel_order_no": "", "tracking_no": tracking, "payment_method": "PREPAID", "payment_type": "pix",
               "installments": "", "order_amount": amount}
        if o.order_status in ("canceled", "unavailable"):
            return oms, STATUS_PENDING, ""
        truth, payer = MATCHED_INDIRECT, buyer
        payments = [(when + pd.Timedelta(minutes=self.rng.randint(3, 180)), amount)]
        shipped = live
        if current:
            injected = self._pick([("split", rates["fb_split"]), ("haggle", rates["fb_haggle"]),
                                   ("duplicate", rates["fb_duplicate"]), ("unpaid", rates["fb_unpaid"]),
                                   ("other", rates["fb_other_payer"]), ("ship", rates["missing_shipment"])])
            first = payments[0][0]
            if injected == "split":
                deposit = float(round(amount * self.rng.uniform(0.3, 0.5)))
                payments = [(first, deposit), (first + pd.Timedelta(hours=self.rng.randint(20, 48)),
                                               round(amount - deposit, 2))]
                truth = Truth("SPLIT_PAYMENT", "PASS_2")
            elif injected == "haggle":
                payments = [(first, round(amount - min(self.rng.choice((5, 10, 15, 20)), amount / 4), 2))]
                truth = Truth("HAGGLED_BELOW_ORDER", "EXCEPTION", "AMOUNT_MISMATCH")
            elif injected == "duplicate":
                payments = [(first, amount), (first + pd.Timedelta(minutes=self.rng.randint(1, 10)), amount)]
                truth = Truth("DUPLICATE_PAYMENT", "EXCEPTION", "DUPLICATE_PAYMENT")
            elif injected == "unpaid":
                payments, truth = [], MISSING_PAYMENT
            elif injected == "other":
                payer, truth = self.name(), Truth("PAYER_NAME_DIFFERS", "REVIEW", "LINK_CANDIDATES")
            elif injected == "ship":
                shipped, truth = False, MISSING_SHIPMENT
        if not live and truth == MATCHED_INDIRECT:
            truth = STATUS_PENDING
        first_ref = ""
        for number, (paid_at, value) in enumerate(payments):
            ref = self._bank(paid_at, f"PIX RECEBIDO - {payer.upper()}", value, kind="pix", payment_type="pix")
            if ref:
                kind = "split" if truth.scenario == "SPLIT_PAYMENT" else "name_amount"
                dup = "DUPLICATE" if truth.scenario == "DUPLICATE_PAYMENT" and number else ""
                self._link("bank", ref, o.order_id, kind, dup or note)
                first_ref = first_ref or ref
        if shipped:
            picked, delivered = self._dates(o)
            self._shipment(o.order_id, tracking, "Correios", self.rng.choice(("PAC", "SEDEX")),
                           labelled=self.rng.random() >= rates["fb_manual_label"], current=current,
                           picked=picked, delivered=delivered, ship_fee=o.freight_total)
        return oms, truth, first_ref

    def platform_terms(self, platform: str, o: Any) -> tuple[str, str, float, float, int, tuple[int, ...]]:
        a = self.a
        if platform == "shopee":
            return (self._unique(lambda: f"{o.order_purchase_timestamp:%y%m%d}{self._code(8)}"),
                    self._unique(lambda: f"BR{self._code(13, DIGITS)}"), a.shopee_commission, a.shopee_fixed_fee,
                    a.shopee_release_days, a.shopee_payout_weekdays)
        return (self._unique(lambda: "57" + self._code(16, DIGITS)), self._unique(lambda: f"TTBR{self._code(12, DIGITS)}"),
                a.tiktok_commission, round(o.item_total * a.tiktok_transaction_fee, 2), a.tiktok_release_days,
                a.tiktok_payout_weekdays)

    def shopee_row(self, row: dict[str, Any], payout_id: str) -> dict[str, Any]:
        return {"ID do pedido": row["order_no"], "ID do repasse": payout_id,
                "Data de criação do pedido": f"{row['created']:%d/%m/%Y %H:%M}",
                "Data de conclusão do pagamento": f"{row['released']:%d/%m/%Y}",
                "Preço original do produto": row["gross"], "Taxa de comissão": row["commission"],
                "Taxa de serviço": row["fees"], "Reembolso": row["refund"], "Valor liberado": row["net"]}

    def payout_line(self, kind: str, label: str, payout_id: str) -> tuple[str, str]:
        if kind == "cod":
            return f"PIX RECEBIDO - {label}", "pix"
        name = "SHOPEE BRASIL" if kind == "shopee" else "TIKTOK SHOP"
        return f"TED RECEBIDA - {name} REPASSE {payout_id}", "ted"

    def non_sales_lines(self) -> list[tuple[str, float, str]]:
        return [(f"TED RECEBIDA - {self.name().upper()} APORTE", float(self.rng.choice((500, 1000, 2000))), "ted"),
                ("RENDIMENTO APLICACAO AUTOMATICA", round(self.rng.uniform(3, 40), 2), "rnd"),
                (f"PIX RECEBIDO - {self.name().upper()} DEVOLUCAO FORNECEDOR", round(self.rng.uniform(40, 300), 2),
                 "pix")]


# --- markets -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Market:
    code: str
    currency: str
    seller: type[_Seller]
    assumptions: Any
    profiles: tuple[Profile, ...]
    policy: dict[str, Any]  # engine policy of the set's config
    simulated: dict[str, str]


MARKETS = {
    "vn": Market("vn", "VND", _VietnamSeller, VietnamAssumptions(), VN_PROFILES,
                 {"mdr_bps": VietnamAssumptions.wallet_mdr_bps,
                  "payment_type_fee_bps": {"bank_transfer": 0, "wallet": VietnamAssumptions.wallet_mdr_bps},
                  "cod_fee_bps": VietnamAssumptions.cod_fee_bps,
                  "payer_pattern": r"^[A-Z]+\.\d+\.(?P<payer>[A-Z ]+?) chuyen tien",
                  "amount_tolerance": 1_000},
                 {"prices": "Olist item prices x price_scale VND/BRL and freight x freight_scale (clipped), "
                            "rounded to 1,000 VND: Vietnamese order values, not the FX rate",
                  "times": "times after purchase compressed by time_scale, so deliveries take a few days",
                  "order_id": "seller order codes ORDyyyy-nnnnnnn; ground_truth.source_order_id is the Olist id",
                  "customer_name": "synthetic Vietnamese names (Olist has none); bank lines print them without "
                                   "diacritics"}),
    "br": Market("br", "BRL", _BrazilSeller, BrazilAssumptions(), BR_PROFILES,
                 {"mdr_bps": round(BrazilAssumptions.card_mdr * 10_000),
                  "cod_fee_bps": round(BrazilAssumptions.courier_cod_fee * 10_000), "amount_tolerance": 0.01,
                  "payer_pattern": r"^PIX RECEBIDO - (?P<payer>.+)$"},
                 {"customer_name": "synthetic names (Olist has none)"}),
}


def build_set(olist: pd.DataFrame, market: Market, profile: Profile,
              seed: int) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    """One set's frames (keyed by file name) and its manifest; `olist` is already localized to the market."""
    rng = random.Random(f"{seed}:{market.code}:{profile.name}")
    start = pd.Timestamp(f"{profile.month}-01")
    next_month = start + pd.DateOffset(months=1)
    seller = market.seller(profile, rng, start, next_month + pd.Timedelta(days=STATEMENT_THROUGH_DAY),
                           market.assumptions)
    placed = olist["order_purchase_timestamp"]
    in_month = olist[(placed >= start) & (placed < next_month)]
    pool = olist[(placed >= start - pd.Timedelta(days=PRIOR_DAYS)) & (placed < next_month)]
    keep = profile.orders / len(in_month)
    channels, weights = list(profile.mix), list(profile.mix.values())
    unit, number = 0, rng.randint(1_000, 9_000)
    for source in pool.itertuples(index=False):
        if rng.random() >= keep:
            continue
        number += 1
        o = source._replace(order_id=f"ORD{source.order_purchase_timestamp:%Y}-{number:07d}") \
            if market.code == "vn" else source
        current = o.order_purchase_timestamp >= start
        channel = rng.choices(channels, weights)[0]
        buyer = seller.name()
        if channel in ("shopee", "tiktok"):
            oms, truth, ref = seller.marketplace(o, current, channel)
        else:
            oms, truth, ref = getattr(seller, channel)(o, current, buyer)
        if not current:
            continue
        unit += 1
        seller.rows["oms"].append({
            "order_id": o.order_id, "channel": channel, "channel_order_no": oms["channel_order_no"],
            "customer_id": o.customer_id, "customer_name": buyer, "tracking_no": oms["tracking_no"],
            "payment_method": oms["payment_method"], "payment_type": oms["payment_type"],
            "installments": oms["installments"], "currency": market.currency, "item_total": o.item_total,
            "freight_total": o.freight_total, "order_amount": oms["order_amount"], "order_status": o.order_status,
            "ordered_at": _iso(o.order_purchase_timestamp)})
        seller.rows["truth"].append({
            "unit_id": unit, "scenario": truth.scenario, "sub_kind": truth.scenario,
            "expected_outcome": truth.outcome, "expected_exception": truth.exception,
            "payment_method": oms["payment_method"], "order_id": o.order_id, "tracking_no": oms["tracking_no"],
            "bank_txn_ref": ref, "channel": channel, "source_order_id": source.order_id})
    seller.flush_payouts()
    seller.non_sales()

    frames = {
        "oms_orders.csv": pd.DataFrame(seller.rows["oms"]),
        "bank_statement.csv": pd.DataFrame(seller.rows["bank"]).sort_values(
            ["value_date", "txn_date", "txn_ref"], kind="stable"),
        "logistics_report.csv": pd.DataFrame(seller.rows["logistics"]).sort_values(
            ["picked_at", "tracking_code"], kind="stable"),
        "shopee_settlement.csv": pd.DataFrame(seller.rows["shopee"]),
        "tiktok_settlement.csv": pd.DataFrame(seller.rows["tiktok"]),
        "ground_truth.csv": pd.DataFrame(seller.rows["truth"]),
        "links.csv": pd.DataFrame(seller.rows["links"]),
    }
    oms, truth = frames["oms_orders.csv"], frames["ground_truth.csv"]
    manifest = {
        "generator": "recon.channeldata", "generator_version": GENERATOR_VERSION, "market": market.code,
        "source": "brazil ecom", "source_files": list(SOURCE_FILES), "seed": seed, "profile": asdict(profile),
        "assumptions": asdict(market.assumptions), "date_shift_days": DATE_SHIFT.days,
        "period": {"orders": [start.date().isoformat(), (next_month - pd.Timedelta(days=1)).date().isoformat()],
                   "statement": [start.date().isoformat(), (seller.end - pd.Timedelta(days=1)).date().isoformat()]},
        "currency": market.currency, "units": len(truth),
        "orders_by_channel": oms["channel"].value_counts().to_dict(),
        "scenario_counts": truth["scenario"].value_counts().to_dict(),
        "settlement_keys": {name: list(keys) for name, keys in seller.SETTLEMENT_KEYS.items()},
        "simulated": {
            "dates": f"Olist dates shifted by {DATE_SHIFT.days} days (418 weeks), so the channels exist in the period",
            "channels": "channels, payment routes, fees, payouts, carriers and settlement reports are simulated; "
                        "order timing, volume shape, statuses and the price spread are Olist's",
            "orders": "a seeded sample of the month's Olist orders across all Olist sellers",
            **market.simulated,
        },
    }
    return frames, manifest


def config_for(market: Market) -> dict[str, Any]:
    """A runnable engine config for one set. No resolutions: the analyst works through Health."""
    return {
        "feeds": {name: {"path": f"{file}.csv", "format": "csv", "encoding": "utf-8", "date_format": None}
                  for name, file in (("oms", "oms_orders"), ("bank", "bank_statement"),
                                     ("logistics", "logistics_report"))},
        "mapping": {
            "oms": {c: c for c in ("order_id", "tracking_no", "payment_method", "order_amount", "ordered_at",
                                   "customer_name")},
            "bank": {c: c for c in ("txn_ref", "description", "credit_amount", "fee_amount", "net_amount",
                                    "value_date")},
            "logistics": {c: c for c in ("tracking_code", "client_order_code", "cod_collected", "ship_fee",
                                         "cod_fee")},
        },
        "policy": {**market.policy,
                   "status_policy": {"delivered": ["payment", "shipment"], "shipped": ["payment", "shipment"]}},
        "resolutions": [],
        "ground_truth": "ground_truth.csv",
        "link_truth": "links.csv",
    }


def baseline(set_dir: str | Path) -> dict[str, Any]:
    """Today's engine on one set, with every Health warning acknowledged so the gate can open."""
    from recon.acknowledgement import acknowledge_warnings
    from recon.evaluation import metrics
    from recon.pipeline import inspect, load_config, run

    set_dir = Path(set_dir)
    config = load_config(set_dir / "config.json")
    report = inspect(set_dir, config)
    config = acknowledge_warnings(config, report, "baseline: every warning acknowledged unread")
    outcome = run(set_dir, config)
    if outcome.blocked:
        return {"status": "BLOCKED", "blocking": [issue.id for issue in outcome.open_issues]}
    scored = metrics(outcome.results)
    links = outcome.manifest["metrics"]["links"]
    return {"status": "COMPLETED", "warnings_acknowledged": len(config.resolutions),
            "decisions": outcome.results["decision"].value_counts().to_dict(),
            "accuracy": scored["accuracy"], "auto_accept_precision": scored["auto_accept_precision"],
            "orphans": int(len(outcome.orphans)),
            "links": {"precision": links["overall"]["precision"], "recall": links["overall"]["recall"],
                      "recall_per_link_kind": {kind: row["recall"] for kind, row in links["per_link_kind"].items()}}}


def generate(source_dir: str | Path, out_dir: str | Path, *, market: str = "vn", seed: int = 42,
             with_baseline: bool = True) -> dict[str, dict[str, Any]]:
    """Write every set of `market` to `out_dir/<market>/<profile name>/`; returns the manifests."""
    chosen = MARKETS[market]
    olist = load_olist(source_dir)
    if chosen.code == "vn":
        olist = localize_vn(olist, chosen.assumptions)
    manifests = {}
    for profile in chosen.profiles:
        frames, manifest = build_set(olist, chosen, profile, seed)
        folder = Path(out_dir) / chosen.code / profile.name
        folder.mkdir(parents=True, exist_ok=True)
        manifest["files"] = {}
        for name, frame in frames.items():
            frame.to_csv(folder / name, index=False)
            manifest["files"][name] = {"rows": len(frame),
                                       "sha256": hashlib.sha256((folder / name).read_bytes()).hexdigest()}
        (folder / "config.json").write_text(json.dumps(config_for(chosen), indent=2) + "\n", encoding="utf-8")
        if with_baseline:
            manifest["baseline"] = baseline(folder)
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str, ensure_ascii=False) + "\n",
                                              encoding="utf-8")
        manifests[profile.name] = manifest
    return manifests


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m recon.channeldata")
    parser.add_argument("--source", default="brazil ecom")
    parser.add_argument("--out", default="test")
    parser.add_argument("--market", choices=sorted(MARKETS), default="vn")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-baseline", action="store_true", help="skip running today's engine on each set")
    args = parser.parse_args()
    for name, manifest in generate(args.source, args.out, market=args.market, seed=args.seed,
                                   with_baseline=not args.no_baseline).items():
        print(name, manifest["units"], manifest["orders_by_channel"], manifest.get("baseline"))
