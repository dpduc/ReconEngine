"""Per-market assumptions and the set profiles built on them."""

from __future__ import annotations

from dataclasses import dataclass


from recon.channeldata.common import Profile


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
# One seller over two consecutive months, for carrying open items from one period into the next.
VN_CHAIN_PROFILES = (
    Profile("d_two_periods", "A website-first seller over two months; period 2 clears period 1's open items.",
            "2026-04", 400, {"website": 0.55, "shopee": 0.20, "tiktok": 0.10, "facebook": 0.15}, _VN_CALM, 2),
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
