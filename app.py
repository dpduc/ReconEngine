from pathlib import Path

import pandas as pd
import streamlit as st

from recon.engine import Policy, artifact_bundle, attach_truth, build_features, load_feeds, metrics, reconcile, validate_feed


ROOT = Path(__file__).parent
DATA = ROOT / "data" / "generated"

st.set_page_config(page_title="SmartRecon", page_icon="🔎", layout="wide")


@st.cache_data
def run_demo(feeds: dict[str, pd.DataFrame], truth: pd.DataFrame | None) -> pd.DataFrame:
    results = reconcile(build_features(feeds), Policy())
    return attach_truth(results, truth) if truth is not None else results


st.sidebar.title("SmartRecon")
st.sidebar.caption("Precision-first reconciliation intelligence")
page = st.sidebar.radio(
    "Walkthrough",
    ["Executive Overview", "Data & Contract", "Match Explorer", "Exceptions / Triage", "Model Diagnostics", "Brazil Olist Explorer"],
)

st.sidebar.caption("Bundled fixtures are the default; upload all three feeds to try a custom run.")
uploads = {
    "oms": st.sidebar.file_uploader("OMS CSV", type="csv"),
    "bank": st.sidebar.file_uploader("Bank CSV", type="csv"),
    "logistics": st.sidebar.file_uploader("Logistics CSV", type="csv"),
}
try:
    if all(uploads.values()):
        feeds = {name: pd.read_csv(upload) for name, upload in uploads.items()}
        errors = [error for name, frame in feeds.items() for error in validate_feed(frame, name)]
        if errors:
            raise ValueError("; ".join(errors))
        results = run_demo(feeds, None)
    else:
        results = run_demo(load_feeds(DATA), pd.read_csv(DATA / "ground_truth.csv"))
except (OSError, ValueError) as exc:
    st.error(f"Unable to load the bundled demo: {exc}")
    st.stop()

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
    st.write("The primary demo is a deterministic September 2026 VND contract with 100,000 scenario units.")
    st.json({"currency": "VND", "amount_tolerance": 1000, "MDR": "180 bps", "COD fee": "80 bps", "cutoff window": "3 days", "policy": ">=99.5% confidence to auto-accept"})
    st.dataframe(results[["order_id", "payment_method", "order_amount", "reason_codes"]].head(100), use_container_width=True)
elif page == "Match Explorer":
    st.title("Match Explorer")
    query = st.text_input("Search order ID or reason code")
    shown = results if not query else results[results.astype(str).apply(lambda col: col.str.contains(query, case=False, na=False)).any(axis=1)]
    st.dataframe(shown.head(500), use_container_width=True)
    for name, content in artifact_bundle(shown).items():
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
    st.title("Brazil Olist Explorer")
    path = ROOT / "brazil ecom" / "olist_orders_dataset.csv"
    orders = pd.read_csv(path)
    st.write("A separate Brazilian marketplace dataset used to demonstrate exploratory data quality—not mixed into the VND reconciliation contract.")
    st.metric("Orders", f"{len(orders):,}")
    st.dataframe(orders.head(200), use_container_width=True)
