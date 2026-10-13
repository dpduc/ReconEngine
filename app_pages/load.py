"""Load: upload one export per feed, review the suggested mapping, confirm or override it."""

import tempfile
from dataclasses import asdict, replace
from pathlib import Path

import streamlit as st

from recon.pipeline import MappingProfile, load_profile, save_profile
from recon_app import state

FEEDS = {"oms": "OMS orders", "bank": "Bank statement", "logistics": "Logistics report"}
TYPES = ["csv", "txt", "tsv", "xlsx", "xlsm", "parquet", "pq"]
UNMAPPED = "(not mapped)"

st.title("Load")
st.caption("Upload the three exports as they come out of each system; nothing is changed in the files.")

columns = st.columns(len(FEEDS))
uploads = {feed: column.file_uploader(label, type=TYPES, key=f"upload_{feed}")
           for column, (feed, label) in zip(columns, FEEDS.items())}
profile_uploads = st.file_uploader(
    "Mapping profiles (optional)", type="json", accept_multiple_files=True, key="upload_profiles",
    help="Profiles saved from an earlier upload; a profile whose columns are all present is reused as-is.")

if not all(uploads.values()):
    if state.is_benchmark():
        st.info("Using the bundled Olist benchmark. Upload all three exports to reconcile your own data.")
    else:
        st.info(f"Using the inputs in {state.inputs()[0]}. Upload all three exports to replace them.")
        st.button("Use the bundled benchmark", icon=":material/restart_alt:", on_click=state.use_benchmark)
    st.page_link("app_pages/health.py", label="Check health", icon=":material/arrow_forward:")
    st.stop()

sources, files = state.stage_uploads({feed: (u.name, u.getvalue()) for feed, u in uploads.items()})
profiles = []
for upload in profile_uploads or []:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "profile.json"
        path.write_bytes(upload.getvalue())
        profiles.append(asdict(load_profile(path)))
draft = state.draft(sources, files, profiles)
key = Path(sources).name  # widget keys follow the uploaded content

st.subheader("Mapping")
st.caption("Each required column is mapped to a source column. Flagged suggestions are left unmapped: "
           "confirm them here or fix them later from the Health Report.")
mapping = {}
for feed, label in FEEDS.items():
    with st.container(border=True):
        st.markdown(f"**{label}** · `{files[feed]}`")
        options = [UNMAPPED, *draft.columns[feed]]
        grid = st.columns(3)
        mapping[feed] = {}
        for i, (canonical, suggestion) in enumerate(draft.suggestions[feed].items()):
            default = draft.config.mapping[feed].get(canonical, UNMAPPED)
            choice = grid[i % 3].selectbox(
                canonical, options, index=options.index(default), key=f"map:{key}:{feed}:{canonical}",
                help=f"Suggested {suggestion.source or 'nothing'} ({suggestion.confidence:.0%}): {suggestion.reason}")
            if suggestion.flagged:
                grid[i % 3].caption(f":orange[Flagged]: {suggestion.reason}"
                                    + (f" · suggested `{suggestion.source}`" if suggestion.source else ""))
            if choice != UNMAPPED:
                mapping[feed][canonical] = choice

if st.button("Confirm mapping", key="confirm_mapping", type="primary", icon=":material/check:"):
    state.set_inputs(sources, replace(draft.config, mapping=mapping))
if state.inputs()[0] == sources:
    st.success("Mapping confirmed. Inspect the feeds next.")
    st.page_link("app_pages/health.py", label="Check health", icon=":material/arrow_forward:")

with st.expander("Save mapping profiles", icon=":material/save:"):
    st.caption("Download a profile per feed to reuse this mapping for the next export from the same system.")
    system = st.text_input("Source system", placeholder="e.g. acme-bank", key="profile_system")
    with st.container(horizontal=True):
        for feed, label in FEEDS.items():
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "profile.json"
                save_profile(MappingProfile(system.strip() or feed, feed, mapping[feed]), path)
                content = path.read_text(encoding="utf-8")
            st.download_button(f"{label} profile", content, f"{system.strip() or 'profile'}_{feed}.json",
                               "application/json", disabled=not system.strip(), key=f"profile_{feed}",
                               icon=":material/download:")
