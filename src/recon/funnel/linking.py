"""Linking passes: attach bank and logistics records to units.

Passes 1 and 2 only enrich; pass 3 (`link_candidates`) sends units with ambiguous or
loose bank candidates to REVIEW.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import pandas as pd

from recon.funnel.base import FunnelContext, StepOutcome, cod_remittance, decide, lineage_prefixed, pass_on
from recon.schema import CURRENCY, CUSTOMER_NAME, ORDER_REF_COLUMNS, PAYER_NAME, REVIEW

ORDER_REF = re.compile(r"(ORD\d{4}-\d{7}|[0-9a-f]{32})", re.IGNORECASE)
CANDIDATE_MAX_SCORE = 0.5  # pass 3 scores stay well below any accept threshold
CANDIDATE_ROWS = "bank_candidate_rows"  # '|'-joined bank source rows listed as pass 3 candidates


def _reference(match: re.Match) -> str:
    """A pattern's order reference: its named group `ref`, else its first group, else the whole match."""
    if "ref" in match.re.groupindex:
        return match.group("ref")
    return match.group(1) if match.re.groups else match.group(0)


def extract_order_ref(description: Any, patterns: tuple[str, ...] = ()) -> str | None:
    """The first order reference found by the chains' `patterns`, in order, then by the default ORDER_REF."""
    text = str(description)
    for pattern in (*(re.compile(p) for p in patterns), ORDER_REF):
        match = pattern.search(text)
        if match:
            return _reference(match)
    return None


def bank_order_refs(bank: pd.DataFrame, patterns: tuple[str, ...] = ()) -> pd.Series:
    """The structured `order_ref` column when mapped, otherwise the reference extracted from the description."""
    if ORDER_REF_COLUMNS["bank"] in bank.columns:
        return bank[ORDER_REF_COLUMNS["bank"]]
    return bank["description"].map(lambda description: extract_order_ref(description, patterns))


def link_one_to_one(units: pd.DataFrame, all_keys: pd.Series, other: pd.DataFrame, unit_key: str,
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
    bank = bank_frame(ctx)
    logistics = lineage_prefixed(ctx.feeds["logistics"], "logistics")
    order_id, orders = ORDER_REF_COLUMNS["oms"], ctx.feeds["oms"][ORDER_REF_COLUMNS["oms"]].dropna()
    linked = link_one_to_one(units, orders, bank, order_id, ORDER_REF_COLUMNS["bank"], ("_oms", "_bank"),
                              "bank_link_pass")
    return link_one_to_one(linked, orders, logistics, order_id, ORDER_REF_COLUMNS["logistics"], ("", "_logistics"),
                            "logistics_link_pass")


def link_exact(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
    """Pass 1: exact order reference, one-to-one only."""
    linked = attach_exact(units, ctx)
    return pass_on(linked)


def bank_frame(ctx: FunnelContext) -> pd.DataFrame:
    bank = lineage_prefixed(ctx.feeds["bank"], "bank")
    bank[ORDER_REF_COLUMNS["bank"]] = bank_order_refs(bank, ctx.order_ref_patterns)
    return bank


def _currency(frame: pd.DataFrame, column: str | None) -> pd.Series:
    if column is None:
        return pd.Series("", index=frame.index)
    return frame[column].fillna("").astype(str).str.strip().str.upper()


def _day(ctx: FunnelContext, feed: str, values: pd.Series) -> pd.Series:
    return ctx.dates(feed, values, utc=True).dt.normalize()


def _candidate_pairs(units: pd.DataFrame, ctx: FunnelContext) -> pd.DataFrame:
    """(unit_id, bank row) pairs for bank-unlinked units against unclaimed bank rows.

    Pairs share a currency (when both feeds carry one) and have the credit within the
    amount tolerance of the expected credit; `day_gap` is |value date - order date|.
    """
    bank = bank_frame(ctx)
    # Rows already linked, naming a payout, or whose reference names a known order (possibly decided
    # upstream), are not free.
    claimed = bank["bank_source_row"].isin(units["bank_source_row"].dropna()) | bank["bank_source_row"].isin(
        ctx.payout_credit_rows)
    names_order = bank[ORDER_REF_COLUMNS["bank"]].isin(ctx.feeds["oms"][ORDER_REF_COLUMNS["oms"]].dropna())
    bank = bank[~claimed & ~names_order]
    open_units = units[units["bank_link_pass"].isna()]
    both_currency = all(CURRENCY in ctx.feeds[f].columns for f in ("oms", "bank"))
    oms_currency = f"{CURRENCY}_oms" if both_currency else None
    expected = open_units["order_amount"]
    if "cod_collected" in open_units.columns:
        cod = open_units["payment_method"].eq("COD") & open_units["cod_collected"].notna()
        expected = expected.where(~cod, cod_remittance(open_units))
    left = pd.DataFrame({"unit_id": open_units["unit_id"], "_currency": _currency(open_units, oms_currency),
                         "_expected": expected, "_day": _day(ctx, "oms", open_units["ordered_at"])})
    right = pd.DataFrame({"bank_pos": bank.index, "_currency": _currency(bank, CURRENCY if both_currency else None),
                          "_credit": bank["credit_amount"], "_value_day": _day(ctx, "bank", bank["value_date"])})
    pairs = left.merge(right, on="_currency")
    pairs = pairs[(pairs["_credit"] - pairs["_expected"]).abs() <= ctx.policy.amount_tolerance]
    pairs = pairs.assign(day_gap=(pairs["_value_day"] - pairs["_day"]).dt.days.abs())
    return pairs[["unit_id", "bank_pos", "day_gap"]].reset_index(drop=True)


def attach_bank(units: pd.DataFrame, bank: pd.DataFrame, links: pd.DataFrame, link_pass: str) -> pd.DataFrame:
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


def normalize_name(name: Any) -> str:
    """A name as compared across feeds: no diacritics, upper case, single spaces ('Bùi Văn Hải' -> 'BUI VAN HAI')."""
    if name is None or (not isinstance(name, str) and pd.isna(name)):
        return ""
    text = str(name).replace("đ", "d").replace("Đ", "D")
    text = "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^0-9A-Za-z]+", " ", text).upper().split())


def payer_names(bank: pd.DataFrame, pattern: str | None) -> pd.Series | None:
    """Each bank row's payer: the payer_name column, else extracted from the description; None without either."""
    if PAYER_NAME in bank.columns:
        return bank[PAYER_NAME]
    if pattern is None:
        return None
    return bank["description"].astype(str).str.extract(pattern)["payer"]


def _payer_differs(pairs: pd.DataFrame, units: pd.DataFrame, bank: pd.DataFrame, ctx: FunnelContext) -> pd.Series:
    """True for pairs whose customer and bank payer both have a name, and the names differ after normalizing."""
    payers = payer_names(bank, ctx.policy.payer_pattern)
    if payers is None or CUSTOMER_NAME not in units.columns:
        return pd.Series(False, index=pairs.index)
    customer = pairs["unit_id"].map(units.set_index("unit_id")[CUSTOMER_NAME]).map(normalize_name)
    payer = payers.loc[pairs["bank_pos"]].map(normalize_name).set_axis(pairs.index)
    return customer.ne("") & payer.ne("") & customer.ne(payer)


def link_constrained(units: pd.DataFrame, ctx: FunnelContext) -> StepOutcome:
    """Pass 2: same currency, amount within tolerance, date within the window; unique on both sides only.

    A credit from a different payer than the order's customer (both named) is never linked here;
    it can still be proposed in pass 3.
    """
    pairs = _candidate_pairs(units, ctx)
    pairs = pairs[pairs["day_gap"] <= ctx.policy.link_date_window_days]
    pairs = pairs[~_payer_differs(pairs, units, bank_frame(ctx), ctx)]
    unique = ~pairs["unit_id"].duplicated(keep=False) & ~pairs["bank_pos"].duplicated(keep=False)
    linked = attach_bank(units, bank_frame(ctx), pairs[unique], "constrained")
    return pass_on(linked)


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
        return pass_on(units)
    bank = bank_frame(ctx)
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
    decided = decide(review, REVIEW, review["unit_id"].map(confidence), reasons)
    return StepOutcome(decided=decided, remaining=remaining)
