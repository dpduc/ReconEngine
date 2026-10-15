"""Canonical schema shared by ingest, checks, the funnel and the UI: the one place for feed-level names."""

FEEDS = ("oms", "bank", "logistics")
FEED_LABELS = {"oms": "OMS orders", "bank": "Bank statement", "logistics": "Logistics report"}

# File extension -> reader format (see ``ingest.READERS``).
FILE_EXTENSIONS = {".csv": "csv", ".txt": "csv", ".tsv": "csv", ".xlsx": "xlsx", ".xlsm": "xlsx",
                   ".parquet": "parquet", ".pq": "parquet"}

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "oms": ("order_id", "tracking_no", "payment_method", "order_amount", "ordered_at"),
    "bank": ("txn_ref", "description", "credit_amount", "fee_amount", "net_amount", "value_date"),
    "logistics": ("tracking_code", "client_order_code", "cod_collected", "ship_fee", "cod_fee"),
}

# Canonical columns parsed as numbers during normalization (optional ones included).
NUMERIC_COLUMNS: dict[str, tuple[str, ...]] = {
    "oms": ("order_amount",),
    "bank": ("credit_amount", "fee_amount", "net_amount"),
    "logistics": ("cod_collected", "ship_fee", "cod_fee"),
}

# The headline amount of each record: what is at risk when it is an exception.
AMOUNT_COLUMNS = {"oms": "order_amount", "bank": "credit_amount", "logistics": "cod_collected"}

# Each record's own key, and the column naming the OMS order it belongs to. The bank's
# `order_ref` is optional: without it the reference is extracted from `description`.
PRIMARY_KEYS = {"oms": "order_id", "bank": "txn_ref", "logistics": "tracking_code"}
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

# Set by resolutions on rows taken out of the run; the pipeline reports and removes them.
EXCLUDED_BY, EXCLUSION_REASON = "excluded_by", "exclusion_reason"

# Canonical (or common pass-through) date columns, first = the record's own date: it orders
# "keep latest" duplicates and dates a record's age in the exception queue.
DATE_COLUMNS: dict[str, tuple[str, ...]] = {
    "oms": ("ordered_at",),
    "bank": ("value_date", "txn_date"),
    "logistics": ("delivered_at", "picked_at"),
}
# The date columns that carry a time of day (screened for placeholder timestamps).
TIMESTAMP_COLUMNS: dict[str, tuple[str, ...]] = {
    "oms": ("ordered_at",), "bank": ("txn_date",), "logistics": ("picked_at", "delivered_at"),
}

# Unit decisions. Every unit ends with exactly one; the exception queue holds the last two.
AUTO_ACCEPT, REVIEW, EXCEPTION = "AUTO_ACCEPT", "REVIEW", "EXCEPTION"
DECISIONS = (AUTO_ACCEPT, REVIEW, EXCEPTION)


def review_column(feed: str) -> str:
    """Column a resolution sets on a feed's rows to route their units to REVIEW (value = reason code)."""
    return f"{feed}_review_reason"
