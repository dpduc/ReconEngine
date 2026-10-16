"""SmartRecon entry point: wizard navigation over ``recon.pipeline``.

Pages live in ``app_pages/`` and share state through ``recon_app.state``. Wizard steps
run in order: Load -> Health -> Fix -> Run -> Review -> Handoff.
"""

import streamlit as st

from recon_app import state

st.set_page_config(page_title="SmartRecon", page_icon=":material/search:", layout="wide")
state.init()

WIZARD = [
    st.Page("app_pages/load.py", title="Load", icon=":material/upload_file:"),
    st.Page("app_pages/health.py", title="Health", icon=":material/health_and_safety:"),
    st.Page("app_pages/fix.py", title="Fix", icon=":material/build:"),
    st.Page("app_pages/run.py", title="Run", icon=":material/play_arrow:"),
    st.Page("app_pages/review.py", title="Review", icon=":material/fact_check:"),
    st.Page("app_pages/handoff.py", title="Handoff", icon=":material/assignment_turned_in:"),
]
BENCHMARK = [
    st.Page("app_pages/overview.py", title="Executive overview", icon=":material/dashboard:"),
    st.Page("app_pages/data_contract.py", title="Data and contract", icon=":material/description:"),
    st.Page("app_pages/diagnostics.py", title="Model diagnostics", icon=":material/monitoring:"),
    st.Page("app_pages/olist_source.py", title="Olist source explorer", icon=":material/storefront:"),
]
page = st.navigation({"Wizard": WIZARD, "Benchmark": BENCHMARK})

st.sidebar.title("SmartRecon")
st.sidebar.caption("Precision-first reconciliation intelligence")

page.run()
