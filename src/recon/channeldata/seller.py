"""The plumbing every market's seller shares."""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pandas as pd

from recon.channeldata.common import (
    ALPHANUMERIC, DIGITS, MATCHED_INDIRECT, PENDING, Profile, STATUS_PENDING, Truth, _date, _iso, _next_weekday,
)


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
    payout_days: dict[str, pd.Timestamp] = field(default_factory=dict)  # payout or remittance id -> paid on

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
            self.payout_days[payout_id] = day
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
