"""Linking passes: attach bank and logistics records to units. Linking decides nothing."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from recon.funnel.base import FunnelContext, StepOutcome, lineage_prefixed

ORDER_REF = re.compile(r"(ORD\d{4}-\d{7}|[0-9a-f]{32})", re.IGNORECASE)


def extract_order_ref(description: Any) -> str | None:
    match = ORDER_REF.search(str(description))
    return match.group(1) if match else None


def _link_one_to_one(units: pd.DataFrame, other: pd.DataFrame, unit_key: str, other_key: str,
                     suffixes: tuple[str, str], link_column: str) -> pd.DataFrame:
    """Left-join `other` where the key is unique on both sides; ambiguous keys stay unlinked."""
    other = other[other[other_key].notna() & ~other[other_key].duplicated(keep=False)]
    linkable = units[unit_key].notna() & ~units[unit_key].duplicated(keep=False)
    keyed = units.assign(_link_key=units[unit_key].where(linkable))
    out = keyed.merge(other, left_on="_link_key", right_on=other_key, how="left",
                      suffixes=suffixes, indicator="_linked")
    out[link_column] = out["_linked"].eq("both").map({True: "exact", False: None})
    return out.drop(columns=["_link_key", "_linked"])


def link_exact(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
    """Pass 1: exact order reference, one-to-one only."""
    bank = lineage_prefixed(ctx.feeds["bank"], "bank")
    bank["order_ref"] = bank["description"].map(extract_order_ref)
    logistics = lineage_prefixed(ctx.feeds["logistics"], "logistics")
    linked = _link_one_to_one(units, bank, "order_id", "order_ref", ("_oms", "_bank"), "bank_link_pass")
    linked = _link_one_to_one(linked, logistics, "order_id", "client_order_code", ("", "_logistics"),
                              "logistics_link_pass")
    return StepOutcome(decided=linked.iloc[0:0], remaining=linked)
