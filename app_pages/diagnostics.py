"""Model diagnostics for the latest run."""

import streamlit as st

from recon_app import state

st.title("Model diagnostics")
outcome = state.require_run()
results, m = outcome.results, outcome.manifest["metrics"]

st.metric("Accuracy against bundled truth", f"{m['accuracy']:.2%}" if m["accuracy"] is not None else "n/a")
st.bar_chart(results.groupby(["reason_codes", "decision"]).size().unstack(fill_value=0),
             alt="Units per reason code and decision")
st.write("Reason codes are the local explanation; the confidence score is calibrated conservatively around the business acceptance threshold.")
st.json(m)
