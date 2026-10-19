"""The Brazilian seller: BRL, a payment gateway, Pix and a local courier."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

import pandas as pd

from recon.channeldata.common import (
    DIGITS, MATCHED_DIRECT, MATCHED_INDIRECT, MISSING_PAYMENT, MISSING_SHIPMENT, PENDING, STATUS_PENDING, Truth,
    _next_weekday,
)
from recon.channeldata.seller import _Seller


BR_FIRST = ("Ana", "Bruno", "Camila", "Daniel", "Eduarda", "Felipe", "Gabriela", "Gustavo", "Helena", "Igor",
            "Juliana", "Lucas", "Larissa", "Marcos", "Mariana", "Natalia", "Otavio", "Paula", "Rafael", "Renata",
            "Sofia", "Thiago", "Vanessa", "Vinicius", "Beatriz", "Carlos", "Fernanda", "Joao", "Leticia", "Pedro")
BR_LAST = ("Silva", "Santos", "Oliveira", "Souza", "Rodrigues", "Ferreira", "Alves", "Pereira", "Lima", "Gomes",
           "Costa", "Ribeiro", "Martins", "Carvalho", "Almeida", "Lopes", "Soares", "Fernandes", "Vieira", "Barbosa")
BR_TOLERANCE = 0.01 + 1e-9


def _s10(rng: random.Random, prefix: str) -> str:
    """A Correios-style tracking code: 2 letters, 8 digits, the S10 check digit, BR."""
    digits = [rng.randrange(10) for _ in range(8)]
    remainder = 11 - sum(d * w for d, w in zip(digits, (8, 6, 4, 2, 3, 5, 9, 7), strict=True)) % 11
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
