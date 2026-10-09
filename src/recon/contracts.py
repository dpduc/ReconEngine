"""Commercial terms shared by the mock data generator and the matching engine.

All money is integer VND. Fees use integer basis points with half-up
rounding so the generator and the engine compute identical values.
"""

from datetime import timedelta, timezone

TZ = timezone(timedelta(hours=7))  # Asia/Ho_Chi_Minh (no DST)
CURRENCY = "VND"

MDR_BPS = 180       # payment gateway MDR: 1.80% of gross, prepaid orders
COD_FEE_BPS = 80    # 3PL COD handling fee: 0.80% of COD collected

AMOUNT_TOLERANCE = 1_000  # Pass 2: |amount delta| <= 1,000 VND is accepted
WINDOW_DAYS = 3           # Pass 2: records may fall up to 3 days outside the period


def fee(amount: int, bps: int) -> int:
    """Contract fee for `amount` at `bps` basis points, rounded half-up."""
    return (amount * bps + 5_000) // 10_000
