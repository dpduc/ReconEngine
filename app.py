import json
import tempfile
from dataclasses import replace
from pathlib import Path

import pandas as pd
import streamlit as st

from recon.config import RunConfig, load_config
from recon.evaluation import metrics
from recon.pipeline import run


ROOT = Path(__file__).parent
DATA = ROOT / "data" / "generated"
CONFIG = ROOT / "configs" / "olist_benchmark.json"

st.set_page_config(page_title="SmartRecon", page_icon="🔎", layout="wide")


@st.cache_data
def run_demo(sources: str, config_json: str):
    """Thin shell over the pipeline: refuse with the open issues if the gate is closed."""
    outcome = run(sources, RunConfig.from_dict(json.loads(config_json)))
    if outcome.blocked:
        raise ValueError("; ".join(issue.message for issue in outcome.open_issues))
    return outcome


st.sidebar.title("SmartRecon")
st.sidebar.caption("Precision-first reconciliation intelligence")
page = st.sidebar.radio(
    "Walkthrough",
    ["Executive Overview", "Data & Contract", "Match Explorer", "Exceptions / Triage", "Model Diagnostics", "Olist Source Explorer"],
)

st.sidebar.caption("Bundled fixtures are the default; upload all three feeds to try a custom run.")
uploads = {
    "oms": st.sidebar.file_uploader("OMS CSV", type="csv"),
    "bank": st.sidebar.file_uploader("Bank CSV", type="csv"),
    "logistics": st.sidebar.file_uploader("Logistics CSV", type="csv"),
}
try:
    config = load_config(CONFIG)
    if all(uploads.values()):
        # Uploads must use canonical column names until the mapping wizard lands.
        folder = Path(tempfile.mkdtemp(prefix="recon-upload-"))
        for name, upload in uploads.items():
            (folder / config.feeds[name].path).write_bytes(upload.getvalue())
        outcome = run_demo(str(folder), replace(config, ground_truth=None).to_json())
    else:
        outcome = run_demo(str(DATA), config.to_json())
except (OSError, ValueError) as exc:
    st.error(f"Unable to run reconciliation: {exc}")
    st.stop()

results = outcome.results
m = metrics(results)

if page == "Executive Overview":
    st.title("SmartRecon")
    st.subheader("Turn messy payment and logistics feeds into trusted cash decisions.")
    st.write("The engine links OMS, bank, and 3PL records, explains every decision, and abstains instead of guessing.")
    cols = st.columns(4)
    cols[0].metric("Units evaluated", f"{m['rows']:,}")
    cols[1].metric("Auto-accepted", f"{m['auto_accepts']:,}")
    cols[2].metric("Review rate", f"{m['review_rate']:.2%}")
    cols[3].metric("Auto-accept precision", f"{m['auto_accept_precision']:.2%}" if m["auto_accept_precision"] is not None else "n/a")
    st.info("Business policy: false matches are more expensive than missed matches, so low-confidence cases remain visible in triage.")
    st.bar_chart(results["reason_codes"].value_counts().head(8))
elif page == "Data & Contract":
    st.title("Data & Contract")
    st.write("The primary demo is a seeded BRL benchmark derived from real Olist orders, items, payments, and delivery events.")
    st.json({"currency": "BRL", "source": "brazil ecom", "amount_tolerance": 0.01, "payment fee assumption": "0 bps: Olist payment values are already settled totals", "policy": ">=99.5% confidence to auto-accept"})
    st.dataframe(results[["order_id", "payment_method", "order_amount", "reason_codes"]].head(100), use_container_width=True)
elif page == "Match Explorer":
    st.title("Match Explorer")
    query = st.text_input("Search order ID or reason code")
    shown = results if not query else results[results.astype(str).apply(lambda col: col.str.contains(query, case=False, na=False)).any(axis=1)]
    st.dataframe(shown.head(500), use_container_width=True)
    for name, content in replace(outcome, results=shown).bundle().items():
        st.download_button(f"Download {name}", content, file_name=name)
elif page == "Exceptions / Triage":
    st.title("Exceptions / Triage")
    queue = results[results["decision"] == "REVIEW"]
    st.caption("Review is intentionally bounded: inspect evidence, adjudicate ambiguous cases, and export the append-only queue.")
    st.dataframe(queue[["order_id", "tracking_no", "reason_codes", "explanation"]].head(500), use_container_width=True)
    st.download_button("Download triage queue", queue.to_csv(index=False), "triage_queue.csv")
elif page == "Model Diagnostics":
    st.title("Model Diagnostics")
    st.metric("Accuracy against bundled truth", f"{m['accuracy']:.2%}" if m["accuracy"] is not None else "n/a")
    st.bar_chart(results.groupby(["reason_codes", "decision"]).size().unstack(fill_value=0))
    st.write("Reason codes are the local explanation; the confidence score is calibrated conservatively around the business acceptance threshold.")
    st.json(m)
else:
    st.title("Olist Source Explorer")
    path = ROOT / "brazil ecom" / "olist_orders_dataset.csv"
    orders = pd.read_csv(path)
    st.write("The real Brazilian marketplace records that anchor the derived BRL reconciliation benchmark.")
    st.metric("Orders", f"{len(orders):,}")
    st.write("The benchmark uses delivered/shipped orders with item and payment totals that tie out in the source data, then applies seeded controlled mutations for evaluation.")
    st.dataframe(orders.head(200), use_container_width=True)
