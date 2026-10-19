"""FX rates for credits in another currency than their order.

A rate converts one unit of the credit currency into the order currency. The bank's own
rate column wins where it has a value; otherwise the rate table's latest rate on or
before the credit's value date is used (a table quoting only the inverse pair works too).
"""

from __future__ import annotations

import io

import pandas as pd

RATE_TABLE_COLUMNS = ("date", "from", "to", "rate")


def read_rate_table(data: bytes) -> pd.DataFrame:
    """A rate table CSV (date, from, to, rate), with each pair also quoted inversely when missing."""
    table = pd.read_csv(io.BytesIO(data), dtype=str)
    missing = set(RATE_TABLE_COLUMNS) - set(table.columns)
    if missing:
        raise ValueError(f"rate table: missing columns {sorted(missing)}")
    table = pd.DataFrame({
        "date": pd.to_datetime(table["date"], format="ISO8601", errors="coerce"),
        "from": table["from"].str.strip().str.upper(),
        "to": table["to"].str.strip().str.upper(),
        "rate": pd.to_numeric(table["rate"], errors="coerce"),
    }).dropna()
    table = table[table["rate"] > 0]
    inverse = table.rename(columns={"from": "to", "to": "from"}).assign(rate=lambda t: 1 / t["rate"])
    quoted = set(zip(table["from"], table["to"], strict=True))
    inverse = inverse[[pair not in quoted for pair in zip(inverse["from"], inverse["to"], strict=True)]]
    return pd.concat([table, inverse], ignore_index=True).sort_values("date", kind="stable")


def lookup_rates(credit_currency: pd.Series, order_currency: pd.Series, value_date: pd.Series,
                 column_rates: pd.Series | None, column_name: str | None,
                 table: pd.DataFrame | None, table_name: str | None) -> pd.DataFrame:
    """Per unit: `fx_rate`, `fx_rate_source` and `fx_rate_date` (NaN/None where no rate applies)."""
    out = pd.DataFrame({"fx_rate": float("nan"), "fx_rate_source": None, "fx_rate_date": None},
                       index=credit_currency.index)
    if column_rates is not None:
        rates = pd.to_numeric(column_rates, errors="coerce")
        has = rates.notna() & (rates > 0)
        out.loc[has, "fx_rate"] = rates[has]
        out.loc[has, "fx_rate_source"] = f"bank column {column_name}"
        out.loc[has, "fx_rate_date"] = value_date[has].dt.strftime("%Y-%m-%d")
    if table is not None:
        need = out["fx_rate"].isna() & value_date.notna()
        if need.any():
            left = pd.DataFrame({"_unit": out.index[need], "date": value_date[need].values,
                                 "from": credit_currency[need].astype(str).values,
                                 "to": order_currency[need].astype(str).values})
            right = table.rename(columns={"date": "rate_date"}).astype({"from": str, "to": str})
            found = pd.merge_asof(left.sort_values("date"), right,
                                  left_on="date", right_on="rate_date", by=["from", "to"],
                                  direction="backward").dropna(subset=["rate"]).set_index("_unit")
            out.loc[found.index, "fx_rate"] = found["rate"]
            out.loc[found.index, "fx_rate_source"] = f"rate table {table_name}"
            out.loc[found.index, "fx_rate_date"] = found["rate_date"].dt.strftime("%Y-%m-%d")
    return out
