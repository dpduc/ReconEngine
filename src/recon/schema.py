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
