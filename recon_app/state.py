"""Wizard state shared by every page, over the public ``recon.pipeline`` API only.

Session keys (the contract between wizard pages):

- ``sources`` (folder path, or uploaded files as ``{relative path: bytes}``) and ``config``
  (RunConfig): the inputs. Default to the bundled Olist benchmark; Load drafts a config from
  uploads (kept in memory, never written to disk) or from a local folder read in place, Fix
  adds resolutions, both through ``set_inputs``.
- ``health`` (HealthReport | None): the latest ``inspect`` result for the inputs. A Health
  page sets it; Run disables itself while it reports a closed gate.
- ``recon_run`` (ReconRun | None): the latest ``run`` result for the inputs.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import streamlit as st

from recon.pipeline import (
    DraftConfig,
    HealthReport,
    MappingProfile,
    ReconRun,
    RunConfig,
    Sources,
    describe_sources,
    draft_config,
    inspect,
    load_config,
    run,
)

ROOT = Path(__file__).parents[1]
BENCHMARK_SOURCES = str(ROOT / "data" / "generated")
BENCHMARK_CONFIG = ROOT / "configs" / "olist_benchmark.json"

SOURCES, CONFIG, HEALTH, RUN = "sources", "config", "health", "recon_run"


def init() -> None:
    """Seed the inputs with the benchmark on first load."""
    if CONFIG not in st.session_state:
        st.session_state[SOURCES] = BENCHMARK_SOURCES
        st.session_state[CONFIG] = load_config(BENCHMARK_CONFIG)
    st.session_state.setdefault(HEALTH, None)
    st.session_state.setdefault(RUN, None)


def inputs() -> tuple[Sources, RunConfig]:
    return st.session_state[SOURCES], st.session_state[CONFIG]


def sources_key(sources: Sources) -> str:
    """Stable identity of `sources` for caching and widget keys: the folder, or a content digest."""
    if not isinstance(sources, Mapping):
        return str(sources)
    digest = hashlib.sha256()
    for path, content in sorted(sources.items()):
        digest.update(path.encode() + b"\0" + hashlib.sha256(content).digest())
    return f"memory:{digest.hexdigest()[:16]}"


def describe_inputs() -> str:
    return "bundled Olist benchmark" if is_benchmark() else describe_sources(inputs()[0])


def set_inputs(sources: Sources, config: RunConfig) -> None:
    """Replace the inputs; health and run results for the old inputs are dropped."""
    current, current_config = inputs()
    if (sources_key(sources), config) != (sources_key(current), current_config):
        st.session_state[SOURCES], st.session_state[CONFIG] = sources, config
        st.session_state[HEALTH] = st.session_state[RUN] = None


def use_benchmark() -> None:
    set_inputs(BENCHMARK_SOURCES, load_config(BENCHMARK_CONFIG))


def upload_sources(files: dict[str, tuple[str, bytes]]) -> tuple[dict[str, bytes], dict[str, str]]:
    """In-memory sources for `{feed: (file name, content)}` uploads, and `{feed: relative path}`.

    Raw bytes are kept exactly as uploaded; nothing is written to disk.
    """
    paths = {feed: f"{feed}/{Path(name).name}" for feed, (name, _) in files.items()}
    return {paths[feed]: content for feed, (_, content) in files.items()}, paths


# Cached on `key` (see `sources_key`); the leading underscore keeps Streamlit from hashing the bytes.
@st.cache_resource(max_entries=4, show_spinner=False)
def _draft(_sources: Sources, key: str, files_json: str, profiles_json: str) -> DraftConfig:
    # Policy comes from the benchmark; uploads start with no resolutions and no ground truth.
    base = replace(load_config(BENCHMARK_CONFIG), resolutions=(), ground_truth=None)
    profiles = [MappingProfile(**p) for p in json.loads(profiles_json)]
    return draft_config(_sources, json.loads(files_json), profiles=profiles, base=base)


def draft(sources: Sources, files: dict[str, str], profiles: list[dict]) -> DraftConfig:
    with st.spinner("Reading exports and suggesting a mapping..."):
        return _draft(sources, sources_key(sources), json.dumps(files, sort_keys=True),
                      json.dumps(profiles, sort_keys=True))


def is_benchmark() -> bool:
    sources, config = inputs()
    return sources_key(sources) == BENCHMARK_SOURCES and config == load_config(BENCHMARK_CONFIG)


@st.cache_resource(max_entries=4, show_spinner=False)
def _inspect(_sources: Sources, key: str, config_json: str) -> HealthReport:
    return inspect(_sources, RunConfig.from_dict(json.loads(config_json)))


@st.cache_resource(max_entries=4, show_spinner=False)
def _run(_sources: Sources, key: str, config_json: str) -> ReconRun:
    return run(_sources, RunConfig.from_dict(json.loads(config_json)))


def check_health() -> HealthReport:
    sources, config = inputs()
    with st.spinner("Inspecting feeds..."):
        st.session_state[HEALTH] = _inspect(sources, sources_key(sources), config.to_json())
    return st.session_state[HEALTH]


def start_run() -> ReconRun:
    sources, config = inputs()
    with st.spinner("Running reconciliation..."):
        outcome = _run(sources, sources_key(sources), config.to_json())
    st.session_state[RUN] = outcome
    st.session_state[HEALTH] = outcome.health
    return outcome


def current_run() -> ReconRun | None:
    """The run for the current inputs; the bundled benchmark runs on first view."""
    if st.session_state[RUN] is None and is_benchmark():
        start_run()
    return st.session_state[RUN]


def require_run() -> ReconRun:
    """Stop the page with a pointer to Run unless a completed run exists."""
    outcome = current_run()
    if outcome is None:
        st.info("No reconciliation has run for these inputs yet.")
        st.page_link("app_pages/run.py", label="Go to run", icon=":material/play_arrow:")
        st.stop()
    if outcome.blocked:
        st.error("The last run was refused: the Health gate is closed.")
        for issue in outcome.open_issues:
            st.markdown(f"- `{issue.id}`: {issue.message}")
        st.stop()
    return outcome


def bundle_zip(outcome: ReconRun) -> bytes:
    """The run's export bundle, file for file, as one zip."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in outcome.bundle().items():
            archive.writestr(name, content)
    return buffer.getvalue()
