"""Trace a decided record back to the canonical source rows it was decided on."""

from __future__ import annotations

import pandas as pd

from recon.schema import FEEDS, ROW_HASH, SOURCE_ROW

AMOUNT_COLUMNS = {"oms": "order_amount", "bank": "credit_amount", "logistics": "cod_collected"}
DELTA_COLUMNS = ("amount_delta", "credit_delta", "cod_amount_delta", "fee_delta", "cod_fee_delta")


def source_rows(record: pd.Series, feeds: dict[str, pd.DataFrame]) -> dict[str, pd.Series | None]:
    """Per feed, the row whose row hash is in the record's ``input_row_hashes`` (None if unlinked).

    Identical raw rows share a hash, so the record's ``<feed>_source_row`` breaks ties.
    """
    hashes = set(str(record.get("input_row_hashes") or "").split("|")) - {""}
    out: dict[str, pd.Series | None] = {}
    for feed in FEEDS:
        frame = feeds.get(feed)
        matches = frame[frame[ROW_HASH].isin(hashes)] if frame is not None else pd.DataFrame()
        source_row = record.get(f"{feed}_{SOURCE_ROW}")
        if len(matches) > 1 and pd.notna(source_row):
            matches = matches[matches[SOURCE_ROW] == source_row]
        out[feed] = matches.iloc[0] if len(matches) else None
    return out
