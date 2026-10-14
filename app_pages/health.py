"""Health: the Health Report for the current inputs, grouped by severity, with profiles."""

import pandas as pd
import streamlit as st

from recon.pipeline import Severity
from recon_app import state

ICONS = {Severity.BLOCKING: ":material/block:", Severity.WARNING: ":material/warning:",
         Severity.INFO: ":material/info:"}

st.title("Health")
st.caption(f"Inputs: {'bundled Olist benchmark' if state.is_benchmark() else state.inputs()[0]}")
health = state.check_health()

if health.gate_open:
    st.success("Health gate open: the feeds can be reconciled.", icon=":material/check_circle:")
else:
    st.error(f"Health gate closed: {len(health.open_issues)} open issue(s). Fix or acknowledge them.",
             icon=":material/block:")

warnings = health.by_severity(Severity.WARNING)
acknowledged = sum(health.is_acknowledged(issue) for issue in warnings)
with st.container(horizontal=True):
    st.metric("Blocking", len(health.blocking_issues), border=True)
    st.metric("Open warnings", len(warnings) - acknowledged, border=True)
    st.metric("Acknowledged", acknowledged, border=True)
    st.metric("Info", len(health.by_severity(Severity.INFO)), border=True)

for severity in Severity:
    issues = health.by_severity(severity)
    if not issues:
        continue
    st.subheader(f"{severity.value.capitalize()} ({len(issues)})")
    for issue in issues:
        done = health.is_acknowledged(issue)
        icon = ":material/check:" if done else ICONS[severity]
        with st.expander(issue.message, icon=icon, expanded=severity is Severity.BLOCKING):
            st.caption(f"`{issue.id}` · {issue.affected_rows:,} affected row(s)")
            if done:
                st.caption(f"Acknowledged: {health.acknowledged[issue.id]}")
            if issue.sample_rows:
                st.dataframe(pd.DataFrame(list(issue.sample_rows)), hide_index=True,
                             alt=f"Sample rows for {issue.id}")

if health.profiles:
    st.subheader("Profiles")
    for name, value in health.profiles.items():
        with st.expander(name.replace("_", " ").capitalize(), icon=":material/bar_chart:"):
            if any(isinstance(v, dict) for v in value.values()):
                table = pd.DataFrame.from_dict(value, orient="index")
            else:
                table = pd.Series(value, name="value").to_frame()
            st.dataframe(table, alt=f"Profile {name}")

with st.container(horizontal=True):
    st.download_button("Health Report (HTML)", health.to_html(), "health_report.html", "text/html",
                       icon=":material/download:")
    st.download_button("Health Report (JSON)", health.to_json(), "health_report.json", "application/json",
                       icon=":material/download:")
st.page_link("app_pages/fix.py", label="Fix issues", icon=":material/arrow_forward:")
