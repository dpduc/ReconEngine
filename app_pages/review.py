"""Review: browse the ranked exception queue (exceptions, review units, orphans), trace any record."""

import pandas as pd
import streamlit as st

from recon.pipeline import AMOUNT_COLUMNS, FEEDS
from recon_app import state
from recon_app.trace import DELTA_COLUMNS, source_rows

st.title("Review")
outcome = state.require_run()

QUEUE = "Exception queue"
WATCHLIST = "Revisit if the period won't close"
VIEWS = {
    QUEUE: lambda: outcome.exception_queue,  # EXCEPTION and REVIEW units plus orphans
    "Needs review": lambda: outcome.exception_queue.query("decision == 'REVIEW'").reset_index(drop=True),
    "Orphans": lambda: outcome.exception_queue.query("record_type != 'oms'").reset_index(drop=True),
    "All units": lambda: outcome.results.assign(record_type="oms", record_ref=outcome.results["order_id"]),
    WATCHLIST: lambda: outcome.close_watchlist.assign(record_type="oms", record_ref=outcome.close_watchlist["order_id"],
                                                      amount_at_risk=outcome.close_watchlist["note_amount"]),
}
with st.container(horizontal=True, vertical_alignment="bottom"):
    view = st.segmented_control("Records", list(VIEWS), default=QUEUE, key="review_view")
    st.download_button("Download bundle", lambda: state.bundle_zip(outcome), file_name="recon_bundle.zip",
                       mime="application/zip", icon=":material/download:", on_click="ignore")
records = VIEWS[view or QUEUE]()
if view == WATCHLIST:
    st.info("Auto-accepted with a note (e.g. installment interest the statement reports). If the period cannot "
            "be closed after every other case is handled, revisit these first, largest amount first.",
            icon=":material/flag:")


def codes(value) -> list[str]:
    return [c for c in str(value).split("|") if c] if isinstance(value, str) else []


with st.container(horizontal=True):
    decision_filter = st.multiselect("Decision", sorted(records["decision"].dropna().unique()),
                                     key="review_decisions")
    code_filter = st.multiselect("Reason code", sorted({c for v in records["reason_codes"] for c in codes(v)}),
                                 key="review_codes")
    step_filter = st.multiselect("Deciding step", sorted(records["deciding_step"].dropna().unique()),
                                 key="review_steps")
    query = st.text_input("Search reference", key="review_query")
if decision_filter:
    records = records[records["decision"].isin(decision_filter)]
if code_filter:
    records = records[records["reason_codes"].map(lambda v: bool(set(codes(v)) & set(code_filter)))]
if step_filter:
    records = records[records["deciding_step"].isin(step_filter)]
if query:
    records = records[records["record_ref"].astype(str).str.contains(query, case=False, regex=False)]
records = records.reset_index(drop=True)

COLUMNS = ["record_type", "record_ref", "amount_at_risk", "age_days", "decision", "reason_codes",
           "deciding_step", "confidence", "link_candidates"]
st.caption(f"{len(records):,} record(s); select a row to trace it.")
table = st.dataframe(records[[c for c in COLUMNS if c in records]], hide_index=True, key="review_table",
                     on_select="rerun", selection_mode="single-row", height=320)
if not len(records):
    st.stop()

selected = table.selection.rows[0] if table.selection.rows else 0
record = records.iloc[min(selected, len(records) - 1)]

st.subheader(f"{record['record_type']} · {record['record_ref']}")
with st.container(border=True):
    st.markdown(f"**{record['decision']}** by `{record['deciding_step']}` · {record['explanation']}")
    deltas = {c: record[c] for c in DELTA_COLUMNS if c in record and pd.notna(record[c])}
    if deltas:
        st.dataframe(pd.DataFrame([deltas]), hide_index=True, key="review_deltas")

rows = source_rows(record, outcome.feeds)
oms_amount = rows["oms"][AMOUNT_COLUMNS["oms"]] if rows["oms"] is not None else None
for column, feed in zip(st.columns(len(FEEDS)), FEEDS):
    with column, st.container(border=True):
        st.markdown(f"**{feed.upper()}**")
        row = rows[feed]
        if row is None:
            st.caption("No linked source row.")
            continue
        amount = row.get(AMOUNT_COLUMNS[feed])
        delta = (amount - oms_amount) if feed != "oms" and pd.notna(amount) and pd.notna(oms_amount) else None
        st.metric(AMOUNT_COLUMNS[feed], f"{amount:,.2f}" if pd.notna(amount) else "n/a",
                  delta=None if delta is None else round(float(delta), 2), delta_color="off")
        st.dataframe(row.rename("value").astype(str).to_frame(), key=f"review_source_{feed}")

per_code = outcome.manifest["metrics"].get("per_reason_code") or {}
if per_code:
    st.subheader("Reason code precision and recall (vs ground truth)")
    st.dataframe(pd.DataFrame.from_dict(per_code, orient="index").rename_axis("reason_code").reset_index(),
                 hide_index=True, key="review_per_code",
                 column_config={"precision": st.column_config.NumberColumn(format="percent"),
                                "recall": st.column_config.NumberColumn(format="percent")})
