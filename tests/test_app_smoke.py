"""Headless smoke check of the Run -> Review wizard on a tiny fixture (the UI has no unit tests)."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from test_pipeline import write_sources

APP = str(Path(__file__).parents[1] / "app.py")


def test_run_then_review_without_exceptions(tmp_path):
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["sources"] = str(tmp_path)
    at.session_state["config"] = write_sources(tmp_path)
    at.run()
    assert not at.exception
    assert at.info  # custom inputs wait for an explicit Run

    at.button[0].click().run()
    assert not at.exception
    run = at.session_state["recon_run"]
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Units"] == "3"
    assert metrics["Exception queue"] == str(len(run.exception_queue))

    at.switch_page("app_pages/review.py").run()
    assert not at.exception
    assert len(at.dataframe(key="review_table").value) == len(run.exception_queue)
    # The top-ranked record is traced to its source rows through row hashes.
    shown = {frame.value.loc["row_hash", "value"] for frame in at.dataframe if "row_hash" in frame.value.index}
    assert shown == set(run.exception_queue.iloc[0]["input_row_hashes"].split("|"))
