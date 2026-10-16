"""Commercial terms shared by the mock data generator and the matching engine.

All money is integer VND. Fees use integer basis points with half-up
rounding so the generator and the engine compute identical values.
"""

from datetime import timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

TZ = timezone(timedelta(hours=7))  # Asia/Ho_Chi_Minh (no DST)
CURRENCY = "VND"

MDR_BPS = 180       # payment gateway MDR: 1.80% of gross, prepaid orders
COD_FEE_BPS = 80    # 3PL COD handling fee: 0.80% of COD collected

AMOUNT_TOLERANCE = 1_000  # Pass 2: |amount delta| <= 1,000 VND is accepted
WINDOW_DAYS = 3           # Pass 2: records may fall up to 3 days outside the period


def fee(amount: int, bps: int) -> int:
    """Contract fee for `amount` at `bps` basis points, rounded half-up."""
    return (amount * bps + 5_000) // 10_000


# Digits of each currency's minor unit (ISO 4217); a code not listed has DEFAULT_MINOR_UNITS.
MINOR_UNITS = {"VND": 0, "JPY": 0, "KRW": 0, "CLP": 0, "ISK": 0, "BHD": 3, "JOD": 3, "KWD": 3, "OMR": 3, "TND": 3}
DEFAULT_MINOR_UNITS = 2


def minor_units(currency: Any) -> int:
    """Minor-unit digits of `currency`; a feed without a currency counts in whole units, as `fee` does."""
    if not isinstance(currency, str) or not currency.strip():
        return 0
    return MINOR_UNITS.get(currency.strip().upper(), DEFAULT_MINOR_UNITS)


def currency_fee(amount: float, bps: int, currency: Any) -> float:
    """Contract fee for `amount` at `bps` basis points, in `currency`'s minor unit, rounded half-up.

    R$ 99.90 at 399 bps is 3.99; 430,000 VND at 110 bps is 4,730 (the same as `fee`).
    """
    digits = minor_units(currency)
    unit = Decimal(1).scaleb(-digits)
    minor = int(Decimal(str(float(amount))).quantize(unit, rounding=ROUND_HALF_UP).scaleb(digits))
    owed = fee(minor, int(bps))
    return owed if digits == 0 else float(Decimal(owed).scaleb(-digits))
