"""Load: pick one export per feed, review the suggested mapping, confirm or override it.

A local folder with its own config.json can be loaded by that config as it is.

Uploads stay in memory; a local folder is read in place. Neither writes copies to disk.
"""

import json
from dataclasses import asdict, replace
from pathlib import Path

import streamlit as st

from recon.pipeline import FEED_LABELS, FILE_EXTENSIONS, MappingProfile, load_config
from recon_app import state

TYPES = [extension.lstrip(".") for extension in FILE_EXTENSIONS]
UNMAPPED = "(not mapped)"
UPLOAD, FOLDER = "Upload files", "Local folder"
CONFIG = "config.json"


def pick_uploads() -> tuple[dict[str, bytes], dict[str, str]] | None:
    columns = st.columns(len(FEED_LABELS))
    uploads = {feed: column.file_uploader(label, type=TYPES, key=f"upload_{feed}")
               for column, (feed, label) in zip(columns, FEED_LABELS.items())}
    if not all(uploads.values()):
        return None
    return state.upload_sources(uploads)


def use_folder_config(path: Path) -> None:
    """Load `path` by its own config.json: feeds, mapping, channels and policy as they are."""
    try:
        config = load_config(path / CONFIG)
    except (ValueError, KeyError, TypeError) as error:
        st.error(f"{CONFIG} can't be read: {error}")
        return
    st.markdown("Feeds: " + ", ".join(f"**{name}** `{spec.path}`" for name, spec in config.feeds.items()))
    if st.button(f"Use {CONFIG}", key="use_folder_config", type="primary", icon=":material/check:"):
        state.set_inputs(str(path), config)
    if state.inputs() == (str(path), config):
        st.success(f"Loaded by its {CONFIG}. Inspect the feeds next.")
        st.page_link("app_pages/health.py", label="Check health", icon=":material/arrow_forward:")


def pick_folder() -> tuple[str, dict[str, str]] | None:
    st.caption("Reads the files where they are, without copying them. "
               "Works only while the app runs on the machine that holds the folder.")
    folder = st.text_input("Folder", placeholder=r"e.g. C:\exports\2026-10", key="local_folder").strip().strip('"')
    if not folder:
        return None
    path = Path(folder)
    if not path.is_dir():
        st.error(f"Not a folder: {folder}")
        return None
    if (path / CONFIG).is_file() and st.toggle(f"Use the folder's {CONFIG}", value=True, key="folder_config",
                                                help="Off: pick the files and confirm a mapping instead."):
        use_folder_config(path)
        st.stop()
    names = sorted(p.name for p in path.iterdir() if p.is_file() and p.suffix.lower() in FILE_EXTENSIONS)
    if not names:
        st.warning(f"No {', '.join(TYPES)} files in {folder}.")
        return None
    columns = st.columns(len(FEED_LABELS))
    files = {feed: column.selectbox(label, names, index=None, key=f"folder_{feed}")
             for column, (feed, label) in zip(columns, FEED_LABELS.items())}
    if not all(files.values()):
        return None
    return str(path), files


st.title("Load")
st.caption("Pick the three exports as they come out of each system; nothing is changed in the files.")

mode = st.segmented_control("Source", [UPLOAD, FOLDER], default=UPLOAD, key="load_mode") or UPLOAD
picked = pick_uploads() if mode == UPLOAD else pick_folder()
profile_uploads = st.file_uploader(
    "Mapping profiles (optional)", type="json", accept_multiple_files=True, key="upload_profiles",
    help="Profiles saved from an earlier load; a profile whose columns are all present is reused as-is.")

if picked is None:
    if state.is_benchmark():
        st.info("Using the bundled Olist benchmark. Pick all three exports to reconcile your own data.")
    else:
        st.info(f"Using {state.describe_inputs()}. Pick all three exports to replace them.")
        st.button("Use the bundled benchmark", icon=":material/restart_alt:", on_click=state.use_benchmark)
    st.page_link("app_pages/health.py", label="Check health", icon=":material/arrow_forward:")
    st.stop()

sources, files = picked
profiles = [asdict(MappingProfile(**json.loads(upload.getvalue()))) for upload in profile_uploads or []]
draft = state.draft(sources, files, profiles)
key = state.sources_key(sources)  # widget keys follow the picked content

st.subheader("Mapping")
st.caption("Each required column is mapped to a source column. Flagged suggestions are left unmapped: "
           "confirm them here or fix them later from the Health Report.")
mapping = {}
for feed, label in FEED_LABELS.items():
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
if state.sources_key(state.inputs()[0]) == key:
    st.success("Mapping confirmed. Inspect the feeds next.")
    st.page_link("app_pages/health.py", label="Check health", icon=":material/arrow_forward:")

with st.expander("Save mapping profiles", icon=":material/save:"):
    st.caption("Download a profile per feed to reuse this mapping for the next export from the same system.")
    system = st.text_input("Source system", placeholder="e.g. acme-bank", key="profile_system")
    with st.container(horizontal=True):
        for feed, label in FEED_LABELS.items():
            profile = MappingProfile(system.strip() or feed, feed, mapping[feed])
            st.download_button(f"{label} profile", json.dumps(asdict(profile), indent=2, sort_keys=True),
                               f"{system.strip() or 'profile'}_{feed}.json", "application/json",
                               disabled=not system.strip(), key=f"profile_{feed}", icon=":material/download:")
