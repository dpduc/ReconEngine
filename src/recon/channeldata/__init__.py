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
today; ``links.csv`` names the true order(s) of every bank line, settlement row and shipment. Both are
written to ``<out>/<market>/labels/<set>/``, apart from the inputs, and the set configs do not name them.
Money due after the statement's last day (the cut-off) is an open item (OPEN).

A chained set (`d_two_periods`) is one seller over consecutive months, one folder per period
(`period_1`, `period_2`): each period's files end at its month end, as they stood at that cut-off,
and period 2 takes period 1's open items (``open_items.csv``, written by running the engine on
period 1) so their money clears them.

    python -m recon.channeldata --out test            # test/vn/<set>/
    python -m recon.channeldata --market br --out test
"""

from recon.channeldata.common import (  # noqa: F401
    ALPHANUMERIC, CHANNELS, DATE_COLUMNS, DATE_SHIFT, DIGITS, FILES, GENERATOR_VERSION, GRACE_DAYS, LABELS,
    LABEL_FILES, MARKETPLACE_SETTLEMENT_DAYS, MATCHED_DIRECT, MATCHED_INDIRECT, MISSING_PAYMENT, MISSING_SHIPMENT,
    PENDING, PRIOR_DAYS, Profile, SHIP_DAYS, SOURCE_FILES, STATEMENT_THROUGH_DAY, STATUS_PENDING, Truth, load_olist,
)
from recon.channeldata.assumptions import (  # noqa: F401
    BR_PROFILES, BrazilAssumptions, VN_CHAIN_PROFILES, VN_PROFILES, VietnamAssumptions,
)
from recon.channeldata.vietnam import VN_BANKS, VN_CARRIERS, VN_FAMILY, VN_GIVEN, VN_MIDDLE, localize_vn  # noqa: F401
from recon.channeldata.brazil import BR_FIRST, BR_LAST, BR_TOLERANCE  # noqa: F401
from recon.channeldata.markets import MARKETS, Market, TIKTOK_REPORT  # noqa: F401
from recon.channeldata.build import build_chain, build_set  # noqa: F401
from recon.channeldata.sets import (  # noqa: F401
    OPEN_ITEMS, baseline, config_for, cutoff_for, generate, labels_for, open_items_of, payment_terms,
)
