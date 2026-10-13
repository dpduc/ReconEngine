"""Wizard state shared by every page. The only pipeline calls are ``inspect`` and ``run``.

Session keys (the contract between wizard pages):

- ``sources`` (str) and ``config`` (RunConfig): the inputs. Default to the bundled Olist
  benchmark; Load/Fix pages replace them through ``set_inputs``.
- ``health`` (HealthReport | None): the latest ``inspect`` result for the inputs. A Health
  page sets it; Run disables itself while it reports a closed gate.
- ``recon_run`` (ReconRun | None): the latest ``run`` result for the inputs.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import streamlit as st

from recon.config import RunConfig, load_config
from recon.health import HealthReport
from recon.pipeline import inspect, run
from recon.results import ReconRun

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


def inputs() -> tuple[str, RunConfig]:
    return st.session_state[SOURCES], st.session_state[CONFIG]


def set_inputs(sources: str, config: RunConfig) -> None:
    """Replace the inputs; health and run results for the old inputs are dropped."""
    if (sources, config) != inputs():
        st.session_state[SOURCES], st.session_state[CONFIG] = sources, config
        st.session_state[HEALTH] = st.session_state[RUN] = None


def is_benchmark() -> bool:
    sources, config = inputs()
    return sources == BENCHMARK_SOURCES and config == load_config(BENCHMARK_CONFIG)


@st.cache_resource(max_entries=4, show_spinner=False)
def _inspect(sources: str, config_json: str) -> HealthReport:
    return inspect(sources, RunConfig.from_dict(json.loads(config_json)))


@st.cache_resource(max_entries=4, show_spinner=False)
def _run(sources: str, config_json: str) -> ReconRun:
    return run(sources, RunConfig.from_dict(json.loads(config_json)))


def check_health() -> HealthReport:
    sources, config = inputs()
    with st.spinner("Inspecting feeds..."):
        st.session_state[HEALTH] = _inspect(sources, config.to_json())
    return st.session_state[HEALTH]


def start_run() -> ReconRun:
    sources, config = inputs()
    with st.spinner("Running reconciliation..."):
        outcome = _run(sources, config.to_json())
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
