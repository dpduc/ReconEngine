"""Data contract behind the benchmark."""

import streamlit as st

from recon_app import state

st.title("Data and contract")
outcome = state.require_run()

st.write("The primary demo is a seeded BRL benchmark derived from real Olist orders, items, payments, and delivery events.")
st.json({"currency": "BRL", "source": "brazil ecom", "amount_tolerance": 0.01,
         "payment fee assumption": "0 bps: Olist payment values are already settled totals",
         "policy": ">=99.5% confidence to auto-accept"})
st.dataframe(outcome.results[["order_id", "payment_method", "order_amount", "reason_codes"]].head(100), hide_index=True)
