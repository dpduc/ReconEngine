"""Fix: pick a fix option or acknowledge each open issue; choices become run-config resolutions."""

from pathlib import Path
from typing import Callable

import pandas as pd
import streamlit as st

from recon.pipeline import (
    ACKNOWLEDGE,
    CONVERT_CURRENCY,
    EXCLUDE_ROWS,
    REMAP,
    SET_DATE_FORMAT,
    Severity,
    acknowledge_warnings,
    resolve,
    undo,
)
from recon_app import state

st.title("Fix")
sources, config = state.inputs()
health = state.check_health()


def apply(new_config, new_sources=None) -> None:
    state.set_inputs(sources if new_sources is None else new_sources, new_config)
    st.rerun()


def remap_params(issue, params: dict) -> bool:
    candidates = params.pop("candidates", [])
    if not candidates:
        st.warning("No unmapped source column is left; reload the right export on the Load page.")
    params["source"] = st.selectbox("Source column", candidates, index=None, key=f"remap:{issue.id}",
                                    placeholder="Choose a column")
    return params["source"] is not None


def date_format_params(issue, params: dict) -> bool:
    candidates = params.pop("candidates", [])
    params["date_format"] = st.selectbox(
        "Date format", candidates, index=0 if candidates else None, accept_new_options=True,
        key=f"date_format:{issue.id}", placeholder="strftime format, e.g. %d/%m/%Y")
    return bool(params["date_format"])


RATE_COLUMN, RATE_TABLE = "Rate column in the bank feed", "Rate table file"


def convert_params(issue, params: dict) -> bool:
    """Choose the rate source: a bank column (the bank's applied rate) and/or a rate table."""
    st.caption("A rate is order-currency units per credit-currency unit. The bank column wins where it has "
               "a value; the table gives the latest rate on or before the credit's value date.")
    source = st.radio("Rate source", [RATE_COLUMN, RATE_TABLE], horizontal=True, key=f"fx_source:{issue.id}")
    if source == RATE_COLUMN:
        params["rate_column"] = st.text_input("Bank column", key=f"fx_column:{issue.id}",
                                              placeholder="e.g. FX Rate").strip() or None
    elif isinstance(sources, dict):
        table = st.file_uploader("Rate table (CSV: date, from, to, rate)", type="csv", key=f"fx_table:{issue.id}")
        params["rate_table"] = f"fx/{table.name}" if table else None
        params.pop("_table_bytes", None)
        if table:
            params["_table_bytes"] = table.getvalue()  # added to the in-memory sources on apply
    else:
        files = sorted(p.name for p in Path(sources).iterdir() if p.is_file() and p.suffix.lower() == ".csv")
        params["rate_table"] = st.selectbox("Rate table in the folder (CSV: date, from, to, rate)", files,
                                            index=None, key=f"fx_table:{issue.id}")
    params["tolerance"] = st.number_input("FX tolerance (%)", 0.0, 10.0, 1.0, 0.1,
                                          key=f"fx_tolerance:{issue.id}") / 100
    return bool(params.get("rate_column") or params.get("rate_table"))


def exclude_params(issue, params: dict) -> bool:
    st.caption(f"{len(params['source_rows']):,} row(s) of '{params['feed']}' will be excluded "
               "and listed in the run's excluded rows.")
    params["reason"] = st.text_input("Reason", key=f"reason:{issue.id}")
    return True


# Fix option id -> widgets that complete its params in place; returns whether they are complete.
# Options not listed here need no input.
OPTION_INPUTS: dict[str, Callable[[object, dict], bool]] = {
    REMAP: remap_params,
    SET_DATE_FORMAT: date_format_params,
    EXCLUDE_ROWS: exclude_params,
    CONVERT_CURRENCY: convert_params,
}


def option_params(issue, option) -> tuple[dict, bool]:
    """Render the widgets the fix option needs; return its params and whether it is complete."""
    params = dict(option.params)
    inputs = OPTION_INPUTS.get(option.id)
    return params, inputs(issue, params) if inputs else True


if health.gate_open:
    st.success("Health gate open: nothing left to fix.", icon=":material/check_circle:")
    st.page_link("app_pages/run.py", label="Run reconciliation", icon=":material/play_arrow:")
else:
    st.error(f"Health gate closed: {len(health.open_issues)} open issue(s).", icon=":material/block:")

open_warnings = [issue for issue in health.open_issues if issue.severity is Severity.WARNING]
if open_warnings:
    with st.container(border=True):
        st.markdown(f"**Acknowledge all {len(open_warnings)} open warning(s)**")
        note = st.text_input("Note", key="acknowledge_all_note",
                             placeholder="Why these warnings are acceptable for this run")
        if st.button("Acknowledge all", key="acknowledge_all", icon=":material/done_all:",
                     disabled=not note.strip()):
            apply(acknowledge_warnings(config, health, note.strip()))

for issue in sorted(health.open_issues, key=lambda i: i.severity is not Severity.BLOCKING):
    with st.container(border=True):
        st.markdown(f"**{issue.message}**")
        st.caption(f"`{issue.id}` · {issue.severity.value} · {issue.affected_rows:,} affected row(s)")
        if issue.sample_rows:
            with st.expander("Sample rows"):
                st.dataframe(pd.DataFrame(list(issue.sample_rows)), hide_index=True,
                             alt=f"Sample rows for {issue.id}")
        labels = [option.label for option in issue.fix_options]
        label = st.selectbox("Fix", labels, key=f"option:{issue.id}")
        option = issue.fix_options[labels.index(label)]
        params, complete = option_params(issue, option)
        note_required = option.id == ACKNOWLEDGE
        note = st.text_input("Note" + (" (required)" if note_required else ""), key=f"note:{issue.id}")
        if st.button("Apply", key=f"apply:{issue.id}", type="primary",
                     disabled=not complete or (note_required and not note.strip())):
            table = params.pop("_table_bytes", None)  # an uploaded rate table joins the in-memory sources
            apply(resolve(config, issue.id, option.id, params, note),
                  {**sources, params["rate_table"]: table} if table is not None else None)

st.subheader("Run config")
if config.resolutions:
    for index, resolution in enumerate(config.resolutions):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown(f"`{resolution.issue_id}` → **{resolution.option}**"
                        + (f" · {resolution.note}" if resolution.note else ""), width="stretch")
            if st.button("Undo", key=f"undo:{index}", icon=":material/undo:", type="tertiary"):
                apply(undo(config, index))
else:
    st.caption("No resolutions yet.")
st.download_button("Download run config", config.to_json(), "run_config.json", "application/json",
                   icon=":material/download:", key="download_config")
with st.expander("Run config (JSON)"):
    st.code(config.to_json(), language="json")
