"""The raw Olist orders the benchmark is derived from."""

import pandas as pd
import streamlit as st

from recon_app import state

st.title("Olist source explorer")
orders = pd.read_csv(state.ROOT / "brazil ecom" / "olist_orders_dataset.csv")
st.write("The real Brazilian marketplace records that anchor the derived BRL reconciliation benchmark.")
st.metric("Orders", f"{len(orders):,}")
st.write("The benchmark uses delivered/shipped orders with item and payment totals that tie out in the source data, then applies seeded controlled mutations for evaluation.")
st.dataframe(orders.head(200), hide_index=True)
