"""Canonical schema shared by ingest, checks, the funnel and the UI: the one place for feed-level names.

Every schema table is keyed by a feed's *kind*: "oms" (the orders role), "bank", "logistics" or
"settlement" (see ``RunConfig.kind``).
"""

# Feed role -> schema kind. Orders, bank and logistics are one feed each, named after their kind;
# settlement reports may be any number, each under its own name.
ROLES = {"orders": "oms", "bank": "bank", "logistics": "logistics", "settlement": "settlement"}
SETTLEMENT = "settlement"
# The single-role feeds, by name: the three a unit is built from.
FEEDS = ("oms", "bank", "logistics")
DEFAULT_ROLES = {kind: role for role, kind in ROLES.items() if kind in FEEDS}
FEED_LABELS = {"oms": "OMS orders", "bank": "Bank statement", "logistics": "Logistics report"}

# File extension -> reader format (see ``ingest.READERS``).
FILE_EXTENSIONS = {".csv": "csv", ".txt": "csv", ".tsv": "csv", ".xlsx": "xlsx", ".xlsm": "xlsx",
                   ".parquet": "parquet", ".pq": "parquet"}

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "oms": ("order_id", "tracking_no", "payment_method", "order_amount", "ordered_at"),
    "bank": ("txn_ref", "description", "credit_amount", "fee_amount", "net_amount", "value_date"),
    "logistics": ("tracking_code", "client_order_code", "cod_collected", "ship_fee", "cod_fee"),
    "settlement": ("order_no", "payout_id", "gross", "net"),
}

# Canonical columns parsed as numbers during normalization (optional ones included).
NUMERIC_COLUMNS: dict[str, tuple[str, ...]] = {
    "oms": ("order_amount",),
    "bank": ("credit_amount", "fee_amount", "net_amount"),
    "logistics": ("cod_collected", "ship_fee", "cod_fee"),
    "settlement": ("gross", "net", "refund"),
}
# Settlement fee columns are named by the report layout's mapping: any canonical `fee_<name>`.
FEE_PREFIX = "fee_"

# The headline amount of each record: what is at risk when it is an exception.
AMOUNT_COLUMNS = {"oms": "order_amount", "bank": "credit_amount", "logistics": "cod_collected", "settlement": "net"}

# Each record's own key, and the column naming the OMS order it belongs to. The bank's
# `order_ref` is optional: without it the reference is extracted from `description`.
PRIMARY_KEYS = {"oms": "order_id", "bank": "txn_ref", "logistics": "tracking_code", "settlement": "order_no"}
ORDER_REF_COLUMNS = {"oms": "order_id", "bank": "order_ref", "logistics": "client_order_code"}

# Lineage carried by every loaded row.
SOURCE_FILE, SOURCE_ROW, ROW_HASH, INGESTED_AT = "source_file", "source_row", "row_hash", "ingested_at"
LINEAGE_COLUMNS = (SOURCE_FILE, SOURCE_ROW, ROW_HASH, INGESTED_AT)

# Optional canonical currency column (any feed); one currency per feed is required.
CURRENCY = "currency"
# Optional bank columns: number of installments, and the interest the statement says the credit includes.
INSTALLMENTS = "installments"
INTEREST_AMOUNT = "interest_amount"
# Optional bank column: the payment rail of a credit (e.g. bank_transfer, wallet); it picks the fee rate.
PAYMENT_TYPE = "payment_type"

# Optional OMS column the status policy is keyed on.
ORDER_STATUS = "order_status"
# Optional payer names: the OMS customer, and the bank payer (a column, or extracted from the description).
CUSTOMER_NAME = "customer_name"
PAYER_NAME = "payer_name"
# Optional OMS columns: the sales channel (it names the money chain), and the channel's own order number.
CHANNEL = "channel"
CHANNEL_ORDER_NO = "channel_order_no"
# Optional logistics columns: the carrier remittance a shipment's COD was paid in, and the shipment status.
REMITTANCE_ID = "remittance_id"
SHIPMENT_STATUS = "status"

# Set by resolutions on rows taken out of the run; the pipeline reports and removes them.
EXCLUDED_BY, EXCLUSION_REASON = "excluded_by", "exclusion_reason"

# Canonical (or common pass-through) date columns, first = the record's own date: it orders
# "keep latest" duplicates and dates a record's age in the exception queue.
DATE_COLUMNS: dict[str, tuple[str, ...]] = {
    "oms": ("ordered_at",),
    "bank": ("value_date", "txn_date"),
    "logistics": ("delivered_at", "picked_at"),
    "settlement": ("settled_at",),
}
# The date columns that carry a time of day (screened for placeholder timestamps).
TIMESTAMP_COLUMNS: dict[str, tuple[str, ...]] = {
    "oms": ("ordered_at",), "bank": ("txn_date",), "logistics": ("picked_at", "delivered_at"),
}

# Unit decisions. Every unit ends with exactly one; the exception queue holds the last two.
AUTO_ACCEPT, REVIEW, EXCEPTION = "AUTO_ACCEPT", "REVIEW", "EXCEPTION"
DECISIONS = (AUTO_ACCEPT, REVIEW, EXCEPTION)


def numeric_columns(kind: str, columns) -> tuple[str, ...]:
    """The numeric canonical columns of a feed of `kind` among `columns`, settlement fee columns included."""
    fees = tuple(c for c in columns if kind == "settlement" and str(c).startswith(FEE_PREFIX))
    return tuple(c for c in NUMERIC_COLUMNS.get(kind, ()) if c in columns) + fees


def review_column(feed: str) -> str:
    """Column a resolution sets on a feed's rows to route their units to REVIEW (value = reason code)."""
    return f"{feed}_review_reason"
