"""Handoff: what still blocks the close, and how well the exception queue is being worked (from the audit log)."""

import pandas as pd
import streamlit as st

from recon.pipeline import close_status, handoff_metrics
from recon_app import state

st.title("Handoff")
outcome = state.require_run()
log = state.audit_log()
log.register_queue(outcome)
metrics = handoff_metrics(outcome, log)
status = close_status(outcome, log)


def percent(value) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def hours(value) -> str:
    return "n/a" if value is None else f"{value:,.1f} h"


st.subheader("Close status")
if status["can_close"]:
    st.success("Nothing in the exception queue blocks closing the period.", icon=":material/task_alt:")
else:
    st.warning(f"{status['unresolved']:,} unresolved record(s), {status['amount_at_risk']:,.2f} at risk, block the close.",
               icon=":material/block:")
watchlist = status["revisit_first"]
with st.container(border=True):
    st.markdown(f"**Revisit first if the period won't close:** {len(watchlist):,} unit(s) auto-accepted with a "
                f"note, {status['revisit_amount']:,.2f} in total, largest first.")
    if len(watchlist):
        st.dataframe(watchlist[[c for c in ("order_id", "reason_codes", "note_amount", "explanation")
                                if c in watchlist]], hide_index=True, height=200, key="handoff_watchlist")
if len(status["blockers"]):
    st.markdown("**Unresolved, by reason code**")
    st.dataframe(status["blockers"], hide_index=True, key="handoff_blockers")

st.subheader("Handoff metrics")
with st.container(horizontal=True):
    st.metric("Resolved", f"{metrics['resolved']:,} / {metrics['queue_items']:,}", border=True)
    st.metric("Override rate", percent(metrics["override_rate"]), border=True,
              help="Share of resolved items the expert matched to another record: engine disagreement.")
    st.metric("Median time to resolution", hours(metrics["median_hours_to_resolution"]), border=True,
              help="From when this queue was first shown to the resolving action.")
    st.metric("First-touch resolution", percent(metrics["first_touch_rate"]), border=True,
              help="Share of resolved items resolved by their only action, never reopened.")
    st.metric("Evidence completeness", percent(metrics["evidence_completeness"]), border=True,
              help="Share of queue items with source rows, an explanation and a recommended action.")

per_code = pd.DataFrame.from_dict(metrics["per_reason_code"], orient="index").rename_axis("reason_code").reset_index()
if len(per_code):
    st.markdown("**Per reason code**")
    st.dataframe(per_code, hide_index=True, key="handoff_per_code",
                 column_config={"override_rate": st.column_config.NumberColumn(format="percent")})
throughput = pd.Series(metrics["throughput_per_day"], name="resolved", dtype=int)
if len(throughput):
    st.markdown("**Throughput per day**")
    st.bar_chart(throughput.rename_axis("day"), alt="Items resolved per day")

entries = log.frame()
entries = entries[entries["run_key"] == metrics["run_key"]]
st.subheader("Audit log")
st.caption(f"Append-only, at `{log.path}`. Queue {metrics['run_key']} first shown {metrics['queued_at']}.")
st.dataframe(entries.dropna(axis=1, how="all"), hide_index=True, key="handoff_log")
st.download_button("Download audit log", entries.to_csv(index=False), "audit_log.csv", "text/csv",
                   icon=":material/download:", on_click="ignore")
