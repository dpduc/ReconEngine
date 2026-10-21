"""Executive overview of the latest run."""

import streamlit as st

from recon_app import state

st.title("ReconEngine")
outcome = state.require_run()
results, m = outcome.results, outcome.manifest["metrics"]

st.subheader("Turn messy payment and logistics feeds into trusted cash decisions.")
st.write("The engine links OMS, bank, and 3PL records, explains every decision, and abstains instead of guessing.")
with st.container(horizontal=True):
    st.metric("Units evaluated", f"{m['rows']:,}", border=True)
    st.metric("Auto-accepted", f"{m['auto_accepts']:,}", border=True)
    st.metric("Review rate", f"{m['review_rate']:.2%}", border=True)
    st.metric("Auto-accept precision",
              f"{m['auto_accept_precision']:.2%}" if m["auto_accept_precision"] is not None else "n/a", border=True)
st.info("Business policy: false matches are more expensive than missed matches, so low-confidence cases remain visible in triage.")
st.bar_chart(results["reason_codes"].value_counts().head(8), alt="Most frequent reason codes")
