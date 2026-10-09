"""Mock data generator for the 3-way reconciliation engine.

Produces three source exports with a seeded, known mix of scenarios:

    oms_orders.csv            internal order system
    bank_statement.csv        payment gateway / bank credits (prepaid + COD remittances)
    logistics_report.csv      3PL delivery report with COD collected and fees

plus ground_truth.csv (the expected engine outcome for every scenario unit)
and manifest.json (parameters, counts, file SHA-256).

Output is byte-for-byte deterministic for a given seed, so regenerating the
same dataset is also a re-import test for the ingestion pipeline.

    python -m recon.mockdata --rows 100000 --seed 42 --out data/generated
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from recon.contracts import (
    AMOUNT_TOLERANCE, COD_FEE_BPS, CURRENCY, MDR_BPS, TZ, WINDOW_DAYS, fee,
)

GENERATOR_VERSION = "1.0.0"

# Share of all scenario units. Each unit is one business transaction
# (or one orphan record).
SCENARIO_MIX = {
    "MATCHED": 0.90,
    "CUTOFF_LAG": 0.05,
    "AMOUNT_ERROR": 0.03,
    "ORPHAN": 0.02,
}
SUB_MIX = {
    "MATCHED": {"MATCHED": 1.0},
    "CUTOFF_LAG": {"LATE_SETTLEMENT": 0.5, "EARLY_ORDER": 0.5},
    "AMOUNT_ERROR": {"ROUNDING": 0.30, "COD_SHORT": 0.30,
                     "MDR_OVERCHARGE": 0.20, "COD_FEE_OVERCHARGE": 0.20},
    "ORPHAN": {"PHANTOM_PAYMENT": 0.35, "PHANTOM_SHIPMENT": 0.25,
               "MISSING_PAYMENT": 0.25, "MISSING_SHIPMENT": 0.15},
}
# sub_kind -> (expected engine outcome, expected exception type)
EXPECTED = {
    "MATCHED": ("PASS_1", ""),
    "LATE_SETTLEMENT": ("PASS_2", ""),
    "EARLY_ORDER": ("PASS_2", ""),
    "ROUNDING": ("PASS_2", ""),
    "COD_SHORT": ("EXCEPTION", "AMOUNT_MISMATCH"),
    "MDR_OVERCHARGE": ("EXCEPTION", "FEE_MISMATCH"),
    "COD_FEE_OVERCHARGE": ("EXCEPTION", "FEE_MISMATCH"),
    "PHANTOM_PAYMENT": ("EXCEPTION", "PHANTOM_PAYMENT"),
    "PHANTOM_SHIPMENT": ("EXCEPTION", "PHANTOM_SHIPMENT"),
    "MISSING_PAYMENT": ("EXCEPTION", "MISSING_PAYMENT"),
    "MISSING_SHIPMENT": ("EXCEPTION", "MISSING_SHIPMENT"),
}
FORCED_METHOD = {
    "ROUNDING": "PREPAID",
    "MDR_OVERCHARGE": "PREPAID",
    "MISSING_SHIPMENT": "PREPAID",
    "COD_SHORT": "COD",
    "COD_FEE_OVERCHARGE": "COD",
}
COD_SHARE = 0.60  # Vietnamese e-commerce is COD-heavy

MIN_AMOUNT = 100_000
MAX_AMOUNT = 20_000_000

PREPAID_MEMOS = ("TT DON HANG {ref}", "{ref} THANH TOAN QUA CONG", "VNPAY {ref} SHOP")
PHANTOM_MEMOS = ("NHAN CK TU KHACH LE", "CK KHONG NOI DUNG", "IBFT NGUYEN VAN A CHUYEN TIEN")


# -----------------------------------------------------------------------------
# Records (normalized: integer VND, aware datetimes)
# -----------------------------------------------------------------------------

@dataclass
class Order:
    order_id: str
    tracking_no: str | None
    customer_id: str
    payment_method: str
    amount: int
    status: str
    ordered_at: datetime


@dataclass
class BankTxn:
    txn_ref: str
    txn_at: datetime
    value_date: date
    description: str
    gross: int
    fee: int
    net: int


@dataclass
class Shipment:
    tracking_no: str
    order_ref: str
    status: str
    cod: int
    ship_fee: int
    cod_fee: int
    remittance_ref: str | None
    picked_at: datetime | None
    delivered_at: datetime | None


@dataclass
class Unit:
    unit_id: int
    scenario: str
    sub_kind: str
    order: Order | None = None
    bank: BankTxn | None = None
    shipment: Shipment | None = None


# -----------------------------------------------------------------------------
# Generation
# -----------------------------------------------------------------------------

def allocate(n: int, mix: dict[str, float]) -> dict[str, int]:
    """Split n into exact integer counts by share (largest-remainder method)."""
    raw = {k: n * share for k, share in mix.items()}
    counts = {k: int(v) for k, v in raw.items()}
    leftover = n - sum(counts.values())
    for k in sorted(raw, key=lambda k: raw[k] - counts[k], reverse=True)[:leftover]:
        counts[k] += 1
    return counts


class Generator:
    def __init__(self, rows: int, seed: int, period_start: date, period_end: date):
        if (period_end - period_start).days < 7:
            raise ValueError("period must span at least 7 days")
        self.rows = rows
        self.rng = random.Random(seed)
        self.start = period_start
        self.end = period_end
        self.prefix = period_start.strftime("%y%m")
        self._txn_seq = 0
        self._rmt_seq = 0

    # -- public ---------------------------------------------------------------

    def build(self) -> list[Unit]:
        sub_kinds: list[tuple[str, str]] = []
        for scenario, n in allocate(self.rows, SCENARIO_MIX).items():
            for sub_kind, m in allocate(n, SUB_MIX[scenario]).items():
                sub_kinds += [(scenario, sub_kind)] * m
        self.rng.shuffle(sub_kinds)
        return [self._unit(i, scenario, sub_kind)
                for i, (scenario, sub_kind) in enumerate(sub_kinds, start=1)]

    # -- primitives -----------------------------------------------------------

    def _amount(self) -> int:
        x = self.rng.lognormvariate(12.6, 0.8)  # median ~300k VND
        return min(MAX_AMOUNT, max(MIN_AMOUNT, int(round(x, -3))))

    def _at(self, d: date, earliest: time = time(8)) -> datetime:
        base = datetime.combine(d, earliest, tzinfo=TZ)
        latest = datetime.combine(d, time(23), tzinfo=TZ)
        return base + timedelta(seconds=self.rng.randrange(int((latest - base).total_seconds())))

    def _days(self, lo: int, hi: int) -> timedelta:
        return timedelta(days=self.rng.randint(lo, hi))

    def _order_id(self, i: int) -> str:
        return f"ORD{self.prefix}-{i:07d}"

    def _tracking(self, i: int) -> str:
        return f"GHN{self.prefix}{i:07d}"

    def _txn_ref(self, d: date) -> str:
        self._txn_seq += 1
        return f"FT{d:%y%m%d}{self._txn_seq:08d}"

    def _remittance_ref(self, d: date) -> str:
        self._rmt_seq += 1
        return f"RMT-{d:%Y%m%d}-{self._rmt_seq:06d}"

    # -- dates per scenario ---------------------------------------------------
    # Pass 1 requires every leg dated inside the period. Normal orders are
    # placed early enough that delivery (+1..3 days) stays inside it.
    # Cut-off orders have at least one leg outside, but within WINDOW_DAYS.

    def _dates(self, sub_kind: str) -> tuple[date, date]:
        """Return (order date, delivery date)."""
        if sub_kind == "LATE_SETTLEMENT":
            ordered = self.end - self._days(0, 1)
            return ordered, self.end + self._days(1, WINDOW_DAYS)
        if sub_kind == "EARLY_ORDER":
            ordered = self.start - self._days(1, WINDOW_DAYS)
            return ordered, self.start + self._days(0, 2)
        span = (self.end - self.start).days - WINDOW_DAYS
        ordered = self.start + self._days(0, span)
        return ordered, ordered + self._days(1, WINDOW_DAYS)

    # -- unit builders --------------------------------------------------------

    def _unit(self, i: int, scenario: str, sub_kind: str) -> Unit:
        unit = Unit(i, scenario, sub_kind)
        if sub_kind == "PHANTOM_PAYMENT":
            unit.bank = self._phantom_payment(i)
            return unit
        if sub_kind == "PHANTOM_SHIPMENT":
            unit.shipment = self._phantom_shipment(i)
            return unit

        method = FORCED_METHOD.get(sub_kind) or (
            "COD" if self.rng.random() < COD_SHARE else "PREPAID")
        order_day, delivery_day = self._dates(sub_kind)
        amount = self._amount()
        order = Order(
            order_id=self._order_id(i),
            tracking_no=self._tracking(i),
            customer_id=f"CUS{self.rng.randrange(1, self.rows // 3 + 2):07d}",
            payment_method=method,
            amount=amount,
            status="COMPLETED",
            ordered_at=self._at(order_day),
        )
        unit.order = order

        if sub_kind == "MISSING_SHIPMENT":
            order.status = "SHIPPED"
        else:
            unit.shipment = self._shipment(order, delivery_day, sub_kind)

        if sub_kind != "MISSING_PAYMENT":
            if method == "PREPAID":
                unit.bank = self._prepaid_payment(order, sub_kind)
            else:
                unit.bank = self._cod_remittance(order, unit.shipment)
        return unit

    def _shipment(self, order: Order, delivery_day: date, sub_kind: str) -> Shipment:
        is_cod = order.payment_method == "COD"
        cod = order.amount if is_cod else 0
        if sub_kind == "COD_SHORT":
            short = max(5 * AMOUNT_TOLERANCE,
                        int(round(order.amount * self.rng.uniform(0.05, 0.30), -3)))
            cod -= short
        cod_fee = fee(cod, COD_FEE_BPS)
        if sub_kind == "COD_FEE_OVERCHARGE":
            cod_fee += max(AMOUNT_TOLERANCE, fee(cod, 50))

        picked = order.ordered_at + timedelta(hours=self.rng.uniform(1, 6))
        delivered = self._at(delivery_day)
        remit = (self._remittance_ref(delivery_day)
                 if is_cod and sub_kind != "MISSING_PAYMENT" else None)
        return Shipment(
            tracking_no=order.tracking_no,
            order_ref=order.order_id,
            status="DELIVERED",
            cod=cod,
            ship_fee=self.rng.randrange(15_000, 45_001, 500),
            cod_fee=cod_fee,
            remittance_ref=remit,
            picked_at=picked,
            delivered_at=delivered,
        )

    def _prepaid_payment(self, order: Order, sub_kind: str) -> BankTxn:
        gross = order.amount
        if sub_kind == "ROUNDING":
            gross += self.rng.choice((-1, 1)) * self.rng.randint(1, AMOUNT_TOLERANCE)
        mdr = fee(gross, MDR_BPS)
        if sub_kind == "MDR_OVERCHARGE":
            mdr += max(AMOUNT_TOLERANCE, fee(gross, 70))

        if sub_kind == "EARLY_ORDER":   # authorized last period, settled this one
            value_date = self.start + self._days(0, 2)
        else:                           # T+0 / T+1 settlement
            value_date = order.ordered_at.date() + self._days(0, 1)

        return BankTxn(
            txn_ref=self._txn_ref(value_date),
            txn_at=order.ordered_at + timedelta(seconds=self.rng.randint(5, 300)),
            value_date=value_date,
            description=self.rng.choice(PREPAID_MEMOS).format(ref=order.order_id),
            gross=gross,
            fee=mdr,
            net=gross - mdr,
        )

    def _cod_remittance(self, order: Order, shipment: Shipment) -> BankTxn:
        # Simplification: the 3PL remits each order on its delivery date.
        # Real carriers remit daily batches (N-M), which the schema supports.
        value_date = shipment.delivered_at.date()
        credit = shipment.cod - shipment.ship_fee - shipment.cod_fee
        end_of_day = datetime.combine(value_date, time(23, 59, 59), tzinfo=TZ)
        remaining = int((end_of_day - shipment.delivered_at).total_seconds())
        return BankTxn(
            txn_ref=self._txn_ref(value_date),
            txn_at=shipment.delivered_at + timedelta(seconds=self.rng.randint(0, remaining)),
            value_date=value_date,
            description=f"GHN CHUYEN COD {shipment.remittance_ref} {order.order_id}",
            gross=credit,
            fee=0,
            net=credit,
        )

    def _phantom_payment(self, i: int) -> BankTxn:
        value_date = self.start + self._days(0, (self.end - self.start).days)
        gross = self._amount()
        if self.rng.random() < 0.5:   # references an order that does not exist
            description = self.rng.choice(PREPAID_MEMOS).format(ref=self._order_id(i))
        else:                         # no usable reference at all
            description = self.rng.choice(PHANTOM_MEMOS)
        return BankTxn(
            txn_ref=self._txn_ref(value_date),
            txn_at=self._at(value_date),
            value_date=value_date,
            description=description,
            gross=gross,
            fee=0,
            net=gross,
        )

    def _phantom_shipment(self, i: int) -> Shipment:
        # The 3PL bills shipping for an order the OMS never created.
        delivered = self._at(self.start + self._days(1, (self.end - self.start).days))
        return Shipment(
            tracking_no=self._tracking(i),
            order_ref=self._order_id(i),
            status="DELIVERED",
            cod=0,
            ship_fee=self.rng.randrange(15_000, 45_001, 500),
            cod_fee=0,
            remittance_ref=None,
            picked_at=delivered - timedelta(days=1),
            delivered_at=delivered,
        )


# -----------------------------------------------------------------------------
# Serialization: each source has its own export format, like real feeds
# -----------------------------------------------------------------------------

OMS_COLUMNS = ["order_id", "tracking_no", "customer_id", "payment_method",
               "currency", "order_amount", "order_status", "ordered_at"]
BANK_COLUMNS = ["txn_ref", "txn_date", "value_date", "description", "currency",
                "credit_amount", "fee_amount", "net_amount"]
LOGISTICS_COLUMNS = ["tracking_code", "client_order_code", "status", "cod_collected",
                     "ship_fee", "cod_fee", "remittance_id", "picked_at", "delivered_at"]
TRUTH_COLUMNS = ["unit_id", "scenario", "sub_kind", "expected_outcome",
                 "expected_exception", "payment_method", "order_id", "tracking_no",
                 "bank_txn_ref"]


def _local(dt: datetime | None, fmt: str) -> str:
    return dt.astimezone(TZ).strftime(fmt) if dt else ""


def oms_row(o: Order) -> list:
    return [o.order_id, o.tracking_no or "", o.customer_id, o.payment_method,
            CURRENCY, o.amount, o.status, o.ordered_at.isoformat()]


def bank_row(b: BankTxn) -> list:
    return [b.txn_ref, _local(b.txn_at, "%d/%m/%Y %H:%M:%S"),
            b.value_date.strftime("%d/%m/%Y"), b.description, CURRENCY,
            b.gross, b.fee, b.net]


def logistics_row(s: Shipment) -> list:
    return [s.tracking_no, s.order_ref, s.status, s.cod, s.ship_fee, s.cod_fee,
            s.remittance_ref or "", _local(s.picked_at, "%Y-%m-%d %H:%M:%S"),
            _local(s.delivered_at, "%Y-%m-%d %H:%M:%S")]


def truth_row(u: Unit) -> list:
    outcome, exception = EXPECTED[u.sub_kind]
    return [u.unit_id, u.scenario, u.sub_kind, outcome, exception,
            u.order.payment_method if u.order else "",
            u.order.order_id if u.order else "",
            u.shipment.tracking_no if u.shipment else "",
            u.bank.txn_ref if u.bank else ""]


def _with_duplicates(rows: list[list], rate: float, rng: random.Random) -> tuple[list[list], int]:
    """Re-send a fraction of lines verbatim, as real feeds do, then shuffle."""
    n_dup = round(len(rows) * rate)
    out = rows + [list(r) for r in rng.sample(rows, n_dup)]
    rng.shuffle(out)
    return out, n_dup


def _write_csv(path: Path, header: list[str], rows: list[list]) -> str:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_dataset(units: list[Unit], out_dir: Path, *, seed: int, period_start: date,
                  period_end: date, dup_rate: float) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(f"{seed}-serialize")

    sources = {
        "oms_orders.csv": (OMS_COLUMNS, [oms_row(u.order) for u in units if u.order]),
        "bank_statement.csv": (BANK_COLUMNS, [bank_row(u.bank) for u in units if u.bank]),
        "logistics_report.csv": (LOGISTICS_COLUMNS,
                                 [logistics_row(u.shipment) for u in units if u.shipment]),
    }
    files = {}
    for name, (header, rows) in sources.items():
        rows, n_dup = _with_duplicates(rows, dup_rate, rng)
        sha = _write_csv(out_dir / name, header, rows)
        files[name] = {"rows": len(rows), "duplicate_rows": n_dup, "sha256": sha}

    sha = _write_csv(out_dir / "ground_truth.csv", TRUTH_COLUMNS, [truth_row(u) for u in units])
    files["ground_truth.csv"] = {"rows": len(units), "duplicate_rows": 0, "sha256": sha}

    manifest = {
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "units": len(units),
        "period": {"start": period_start.isoformat(), "end": period_end.isoformat()},
        "contract": {"mdr_bps": MDR_BPS, "cod_fee_bps": COD_FEE_BPS,
                     "amount_tolerance": AMOUNT_TOLERANCE, "window_days": WINDOW_DAYS,
                     "currency": CURRENCY},
        "duplicate_rate": dup_rate,
        "scenarios": dict(sorted(Counter(u.scenario for u in units).items())),
        "sub_kinds": dict(sorted(Counter(u.sub_kind for u in units).items())),
        "files": files,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def generate(rows: int, seed: int, period_start: date, period_end: date,
             out_dir: Path, dup_rate: float = 0.005) -> dict:
    units = Generator(rows, seed, period_start, period_end).build()
    return write_dataset(units, out_dir, seed=seed, period_start=period_start,
                         period_end=period_end, dup_rate=dup_rate)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--rows", type=int, default=100_000, help="scenario units to generate")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--period-start", type=date.fromisoformat, default=date(2026, 9, 1))
    p.add_argument("--period-end", type=date.fromisoformat, default=date(2026, 9, 30))
    p.add_argument("--dup-rate", type=float, default=0.005,
                   help="fraction of lines re-sent verbatim in each source file")
    p.add_argument("--out", type=Path, default=Path("data/generated"))
    args = p.parse_args(argv)

    manifest = generate(args.rows, args.seed, args.period_start, args.period_end,
                        args.out, args.dup_rate)
    print(json.dumps({"scenarios": manifest["scenarios"],
                      "files": {k: v["rows"] for k, v in manifest["files"].items()}},
                     indent=2))


if __name__ == "__main__":
    main()
