"""Run: execute the pipeline once the Health gate is open; show funnel counts per step."""

import streamlit as st

from recon_app import state

st.title("Run")
sources, config = state.inputs()
st.caption(f"Inputs: {'bundled Olist benchmark' if state.is_benchmark() else sources}")

health = st.session_state[state.HEALTH]
gate_closed = health is not None and not health.gate_open
if gate_closed:
    st.error(f"Health gate closed: {len(health.open_issues)} open issue(s). Resolve them before running.")
if st.button("Run reconciliation", type="primary", icon=":material/play_arrow:", disabled=gate_closed):
    state.start_run()

outcome = state.current_run()
if outcome is None:
    st.info("Press Run to reconcile the current inputs.")
    st.stop()
if outcome.blocked:
    st.error("Run refused: the Health gate is closed.")
    for issue in outcome.open_issues:
        st.markdown(f"- `{issue.id}`: {issue.message}")
    st.stop()

summary = outcome.manifest["metrics"]
queue = outcome.exception_queue
with st.container(horizontal=True):
    st.metric("Units", f"{summary['rows']:,}", border=True)
    st.metric("Auto-accepted", f"{summary['auto_accepts']:,}", border=True)
    st.metric("Review queue", f"{summary['review_queue']:,}", border=True)
    st.metric("Exception queue", f"{len(queue):,}", border=True)
    st.metric("Orphans", f"{len(outcome.orphans):,}", border=True)

st.subheader("Funnel")
st.dataframe(outcome.funnel, hide_index=True, key="run_funnel")
st.bar_chart(outcome.funnel.set_index("step")[["decided", "passed_on"]], stack=True,
             alt="Units decided and passed on at each funnel step")
st.caption(f"Rule version {outcome.manifest['rule_version']} · run at {outcome.manifest['created_at']}")
st.page_link("app_pages/review.py", label="Review exceptions", icon=":material/arrow_forward:")
