"""SmartRecon entry point: wizard navigation over ``recon.pipeline``.

Pages live in ``app_pages/`` and share state through ``recon_app.state``. Wizard steps
run in order; Load -> Health -> Fix slot into ``WIZARD`` ahead of Run.
"""

import hashlib
import tempfile
from dataclasses import replace
from pathlib import Path

import streamlit as st

from recon.config import load_config
from recon_app import state

st.set_page_config(page_title="SmartRecon", page_icon=":material/search:", layout="wide")
state.init()

WIZARD = [
    st.Page("app_pages/run.py", title="Run", icon=":material/play_arrow:"),
    st.Page("app_pages/review.py", title="Review", icon=":material/fact_check:"),
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
# Until the Load step lands: canonical-column CSV uploads replace the bundled benchmark.
st.sidebar.caption("Bundled fixtures are the default; upload all three feeds to try a custom run.")
uploads = {
    "oms": st.sidebar.file_uploader("OMS CSV", type="csv"),
    "bank": st.sidebar.file_uploader("Bank CSV", type="csv"),
    "logistics": st.sidebar.file_uploader("Logistics CSV", type="csv"),
}
benchmark = load_config(state.BENCHMARK_CONFIG)
if all(uploads.values()):
    digest = hashlib.sha256(b"".join(upload.getvalue() for upload in uploads.values())).hexdigest()[:16]
    folder = Path(tempfile.gettempdir()) / f"recon-upload-{digest}"
    folder.mkdir(exist_ok=True)
    for name, upload in uploads.items():
        (folder / benchmark.feeds[name].path).write_bytes(upload.getvalue())
    state.set_inputs(str(folder), replace(benchmark, ground_truth=None))
    st.session_state["sidebar_uploads"] = True
elif st.session_state.pop("sidebar_uploads", False):
    state.set_inputs(state.BENCHMARK_SOURCES, benchmark)

page.run()
