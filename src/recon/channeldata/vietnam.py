"""The Vietnamese seller: VND, COD carriers, wallets and bank transfers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from recon.contracts import fee as vnd_fee
from recon.channeldata.common import (
    DATE_COLUMNS, DIGITS, MATCHED_DIRECT, MATCHED_INDIRECT, MISSING_PAYMENT, MISSING_SHIPMENT, PENDING,
    STATUS_PENDING, Truth, _ascii, _next_weekday,
)
from recon.channeldata.assumptions import VietnamAssumptions
from recon.channeldata.seller import _Seller


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
