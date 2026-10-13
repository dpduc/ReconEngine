"""Linking passes: attach bank and logistics records to units.

Passes 1 and 2 only enrich; pass 3 (`link_candidates`) sends units with ambiguous or
loose bank candidates to REVIEW.
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from recon.funnel.base import FunnelContext, StepOutcome, decide, lineage_prefixed

ORDER_REF = re.compile(r"(ORD\d{4}-\d{7}|[0-9a-f]{32})", re.IGNORECASE)
CANDIDATE_MAX_SCORE = 0.5  # pass 3 scores stay well below any accept threshold
CANDIDATE_ROWS = "bank_candidate_rows"  # '|'-joined bank source rows listed as pass 3 candidates


def extract_order_ref(description: Any) -> str | None:
    match = ORDER_REF.search(str(description))
    return match.group(1) if match else None


def bank_order_refs(bank: pd.DataFrame) -> pd.Series:
    """The structured `order_ref` column when mapped, otherwise the reference extracted from the description."""
    if "order_ref" in bank.columns:
        return bank["order_ref"]
    return bank["description"].map(extract_order_ref)


def _link_one_to_one(units: pd.DataFrame, all_keys: pd.Series, other: pd.DataFrame, unit_key: str,
                     other_key: str, suffixes: tuple[str, str], link_column: str) -> pd.DataFrame:
    """Left-join `other` where the key is unique on both sides; ambiguous keys stay unlinked.

    Uniqueness on the unit side is judged over `all_keys` (every OMS order), so a subset of
    units links exactly as it would together with the rest.
    """
    other = other[other[other_key].notna() & ~other[other_key].duplicated(keep=False)]
    ambiguous = all_keys[all_keys.duplicated(keep=False)]
    linkable = units[unit_key].notna() & ~units[unit_key].isin(ambiguous)
    keyed = units.assign(_link_key=units[unit_key].where(linkable))
    out = keyed.merge(other, left_on="_link_key", right_on=other_key, how="left",
                      suffixes=suffixes, indicator="_linked")
    out[link_column] = out["_linked"].eq("both").map({True: "exact", False: None})
    return out.drop(columns=["_link_key", "_linked"])


def attach_exact(units: pd.DataFrame, ctx: FunnelContext) -> pd.DataFrame:
    """`units` enriched with the bank and logistics records naming them (one-to-one only)."""
    bank = _bank_frame(ctx)
    logistics = lineage_prefixed(ctx.feeds["logistics"], "logistics")
    orders = ctx.feeds["oms"]["order_id"].dropna()
    linked = _link_one_to_one(units, orders, bank, "order_id", "order_ref", ("_oms", "_bank"), "bank_link_pass")
    return _link_one_to_one(linked, orders, logistics, "order_id", "client_order_code", ("", "_logistics"),
                            "logistics_link_pass")


def link_exact(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
    """Pass 1: exact order reference, one-to-one only."""
    linked = attach_exact(units, ctx)
    return StepOutcome(decided=linked.iloc[0:0], remaining=linked)


def _bank_frame(ctx: FunnelContext) -> pd.DataFrame:
    bank = lineage_prefixed(ctx.feeds["bank"], "bank")
    bank["order_ref"] = bank_order_refs(bank)
    return bank


def _currency(frame: pd.DataFrame, column: str | None) -> pd.Series:
    if column is None:
        return pd.Series("", index=frame.index)
    return frame[column].fillna("").astype(str).str.strip().str.upper()


def _day(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values, errors="coerce", format="mixed", utc=True).dt.normalize()


def _candidate_pairs(units: pd.DataFrame, ctx: FunnelContext) -> pd.DataFrame:
    """(unit_id, bank row) pairs for bank-unlinked units against unclaimed bank rows.

    Pairs share a currency (when both feeds carry one) and have the credit within the
    amount tolerance of the expected credit; `day_gap` is |value date - order date|.
    """
    bank = _bank_frame(ctx)
    # Rows already linked, or whose reference names a known order (possibly decided upstream), are not free.
    claimed = bank["bank_source_row"].isin(units["bank_source_row"].dropna())
    names_order = bank["order_ref"].isin(ctx.feeds["oms"]["order_id"].dropna())
    bank = bank[~claimed & ~names_order]
    open_units = units[units["bank_link_pass"].isna()]
    both_currency = all("currency" in ctx.feeds[f].columns for f in ("oms", "bank"))
    oms_currency = "currency_oms" if both_currency else None
    expected = open_units["order_amount"]
    if "cod_collected" in open_units.columns:
        remittance = open_units["cod_collected"] - open_units["ship_fee"].fillna(0) - open_units["cod_fee"].fillna(0)
        cod = open_units["payment_method"].eq("COD") & open_units["cod_collected"].notna()
        expected = expected.where(~cod, remittance)
    left = pd.DataFrame({"unit_id": open_units["unit_id"], "_currency": _currency(open_units, oms_currency),
                         "_expected": expected, "_day": _day(open_units["ordered_at"])})
    right = pd.DataFrame({"bank_pos": bank.index, "_currency": _currency(bank, "currency" if both_currency else None),
                          "_credit": bank["credit_amount"], "_value_day": _day(bank["value_date"])})
    pairs = left.merge(right, on="_currency")
    pairs = pairs[(pairs["_credit"] - pairs["_expected"]).abs() <= ctx.policy.amount_tolerance]
    pairs = pairs.assign(day_gap=(pairs["_value_day"] - pairs["_day"]).dt.days.abs())
    return pairs[["unit_id", "bank_pos", "day_gap"]].reset_index(drop=True)


def _attach_bank(units: pd.DataFrame, bank: pd.DataFrame, links: pd.DataFrame, link_pass: str) -> pd.DataFrame:
    """Copy the linked bank row into each unit in `links` (unit_id -> bank_pos), as pass 1 named the columns."""
    out = units.copy()
    rows = out["unit_id"].map(links.set_index("unit_id")["bank_pos"])
    mask = rows.notna()
    matched = bank.loc[rows[mask].astype(int)]
    for column in bank.columns:
        target = f"{column}_bank" if f"{column}_bank" in out.columns else column
        out.loc[mask, target] = matched[column].to_numpy()
    out.loc[mask, "bank_link_pass"] = link_pass
    return out


def link_constrained(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
    """Pass 2: same currency, amount within tolerance, date within the window; unique on both sides only."""
    pairs = _candidate_pairs(units, ctx)
    pairs = pairs[pairs["day_gap"] <= ctx.policy.link_date_window_days]
    unique = ~pairs["unit_id"].duplicated(keep=False) & ~pairs["bank_pos"].duplicated(keep=False)
    linked = _attach_bank(units, _bank_frame(ctx), pairs[unique], "constrained")
    return StepOutcome(decided=linked.iloc[0:0], remaining=linked)


def link_candidates(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
    """Pass 3: units still without a bank record but with plausible bank rows go to REVIEW, never AUTO_ACCEPT.

    Candidates share currency and amount (within tolerance) and fall within twice the date
    window. Each scores CANDIDATE_MAX_SCORE scaled down by its date gap; the unit's confidence
    is the best score divided by the number of candidates. `link_candidates` lists
    `txn_ref:score` pairs, best first.
    """
    window = ctx.policy.link_date_window_days
    pairs = _candidate_pairs(units, ctx)
    pairs = pairs[pairs["day_gap"] <= 2 * window]
    if pairs.empty:
        return StepOutcome(decided=units.iloc[0:0], remaining=units)
    bank = _bank_frame(ctx)
    pairs = pairs.assign(txn_ref=bank.loc[pairs["bank_pos"], "txn_ref"].to_numpy(),
                         score=(CANDIDATE_MAX_SCORE * (1 - pairs["day_gap"] / (2 * window + 1))).round(4))
    pairs = pairs.assign(source_row=bank.loc[pairs["bank_pos"], "bank_source_row"].to_numpy())
    pairs = pairs.sort_values(["unit_id", "score", "txn_ref"], ascending=[True, False, True])
    grouped = pairs.groupby("unit_id")
    listed = grouped.apply(lambda g: "|".join(f"{t}:{s:.4f}" for t, s in zip(g["txn_ref"], g["score"])),
                           include_groups=False)
    candidate_rows = grouped["source_row"].agg(lambda rows: "|".join(str(int(r)) for r in rows))
    confidence = (grouped["score"].max() / grouped.size()).round(4)

    has_candidates = units["unit_id"].isin(listed.index)
    review, remaining = units[has_candidates].copy(), units[~has_candidates]
    review["link_candidates"] = review["unit_id"].map(listed)
    review[CANDIDATE_ROWS] = review["unit_id"].map(candidate_rows)
    reasons = review["logistics_link_pass"].isna().map({True: "LINK_CANDIDATES|MISSING_SHIPMENT",
                                                        False: "LINK_CANDIDATES"})
    decided = decide(review, "REVIEW", review["unit_id"].map(confidence), reasons)
    return StepOutcome(decided=decided, remaining=remaining)
