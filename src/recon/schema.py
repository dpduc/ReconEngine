"""Canonical schema shared by ingest, checks and the funnel."""

FEEDS = ("oms", "bank", "logistics")

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

PRIMARY_KEYS = {"oms": "order_id", "bank": "txn_ref", "logistics": "tracking_code"}

# Lineage carried by every loaded row.
SOURCE_FILE, SOURCE_ROW, ROW_HASH, INGESTED_AT = "source_file", "source_row", "row_hash", "ingested_at"
LINEAGE_COLUMNS = (SOURCE_FILE, SOURCE_ROW, ROW_HASH, INGESTED_AT)

# Optional canonical currency column (any feed); one currency per feed is required.
CURRENCY = "currency"

# Set by resolutions on rows taken out of the run; the pipeline reports and removes them.
EXCLUDED_BY, EXCLUSION_REASON = "excluded_by", "exclusion_reason"

# Canonical (or common pass-through) date columns, first = the record's own date ("latest" order).
DATE_COLUMNS: dict[str, tuple[str, ...]] = {
    "oms": ("ordered_at",),
    "bank": ("value_date", "txn_date"),
    "logistics": ("delivered_at", "picked_at"),
}


# Unit decisions. Every unit ends with exactly one; the work queue holds the last two.
AUTO_ACCEPT, REVIEW, EXCEPTION = "AUTO_ACCEPT", "REVIEW", "EXCEPTION"
DECISIONS = (AUTO_ACCEPT, REVIEW, EXCEPTION)


def review_column(feed: str) -> str:
    """Column a resolution sets on a feed's rows to route their units to REVIEW (value = reason code)."""
    return f"{feed}_review_reason"
