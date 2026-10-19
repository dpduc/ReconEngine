"""The markets a set can be built for."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


from recon.channeldata.common import Profile
from recon.channeldata.assumptions import BR_PROFILES, BrazilAssumptions, VN_PROFILES, VietnamAssumptions
from recon.channeldata.seller import _Seller
from recon.channeldata.vietnam import _VietnamSeller
from recon.channeldata.brazil import _BrazilSeller


@dataclass(frozen=True)
class Market:
    code: str
    currency: str
    seller: type[_Seller]
    assumptions: Any
    profiles: tuple[Profile, ...]
    policy: dict[str, Any]  # engine policy of the set's config
    simulated: dict[str, str]
    # marketplace -> its settlement report feed in the set's config: file, date format, column mapping
    # and the channel's terms (fee_bps, amount_tolerance)
    reports: dict[str, dict[str, Any]] = field(default_factory=dict)
    # channels whose COD comes back in carrier remittances that the bank credit names
    cod_remittances: tuple[str, ...] = ()


TIKTOK_REPORT = {"path": "tiktok_settlement.csv", "date_format": "%Y/%m/%d %H:%M:%S",
                 "mapping": {"order_no": "Order ID", "payout_id": "Statement ID", "gross": "Total revenue",
                             "fee_commission": "Platform commission", "fee_transaction": "Transaction fee",
                             "refund": "Refund", "net": "Total settlement amount", "settled_at": "Order settled time"}}


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
                                   "diacritics"},
                 {"shopee": {"path": "shopee_settlement.csv", "date_format": "%d/%m/%Y",
                             "mapping": {"order_no": "Mã đơn hàng", "payout_id": "Mã thanh toán",
                                         "gross": "Giá sản phẩm", "fee_fixed": "Phí cố định",
                                         "fee_payment": "Phí thanh toán", "refund": "Hoàn tiền",
                                         "net": "Tổng tiền đã thanh toán", "settled_at": "Ngày hoàn thành thanh toán"},
                             # reports are exact to the dong: the tolerance is rounding, not a customer's typo
                             "terms": {"fee_bps": round((VietnamAssumptions.shopee_fixed_rate
                                                         + VietnamAssumptions.shopee_payment_rate) * 10_000),
                                       "amount_tolerance": 1}},
                  "tiktok": {**TIKTOK_REPORT,
                             "terms": {"fee_bps": round((VietnamAssumptions.tiktok_commission
                                                         + VietnamAssumptions.tiktok_transaction_fee) * 10_000),
                                       "amount_tolerance": 1}}},
                 cod_remittances=("website", "facebook")),
    "br": Market("br", "BRL", _BrazilSeller, BrazilAssumptions(), BR_PROFILES,
                 {"mdr_bps": round(BrazilAssumptions.card_mdr * 10_000),
                  "cod_fee_bps": round(BrazilAssumptions.courier_cod_fee * 10_000), "amount_tolerance": 0.01,
                  "payer_pattern": r"^PIX RECEBIDO - (?P<payer>.+)$"},
                 {"customer_name": "synthetic names (Olist has none)"},
                 {"shopee": {"path": "shopee_settlement.csv", "date_format": "%d/%m/%Y",
                             "mapping": {"order_no": "ID do pedido", "payout_id": "ID do repasse",
                                         "gross": "Preço original do produto", "fee_commission": "Taxa de comissão",
                                         "fee_service": "Taxa de serviço", "refund": "Reembolso",
                                         "net": "Valor liberado", "settled_at": "Data de conclusão do pagamento"},
                             "terms": {}},  # a fixed fee per order on top of the commission: not a rate
                  "tiktok": {**TIKTOK_REPORT,
                             "terms": {"fee_bps": round((BrazilAssumptions.tiktok_commission
                                                         + BrazilAssumptions.tiktok_transaction_fee) * 10_000)}}}),
}
