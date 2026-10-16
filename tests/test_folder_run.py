"""A folder runs from its own config.json; labels come in only from a folder of their own."""

import io
import json
from dataclasses import replace
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from recon.channeldata import LABEL_FILES, labels_for
from recon.cli import GATE_CLOSED, NO_CONFIG, main, run_folder
from recon.pipeline import load_config, run

from test_pipeline import MAPPING, acknowledged, write_sources

ROOT = Path(__file__).parents[1]
SOCIAL = ROOT / "test" / "vn" / "c_social_heavy"
APP = str(ROOT / "app.py")


def folder_run(folder: Path, out_dir: Path, labels: Path | None = None, note: str | None = None) -> tuple[int, str]:
    printed = io.StringIO()
    return run_folder(folder, labels, out_dir, note, out=printed), printed.getvalue()


def test_a_folder_runs_from_its_config_and_is_scored_on_its_labels(tmp_path):
    code, printed = folder_run(SOCIAL, tmp_path / "out", labels_for(SOCIAL), note="seen in review")

    assert code == 0, printed
    assert "Accuracy: 100.0%" in printed and "Links: precision" in printed
    assert {"decisions.csv", "exception_queue.csv", "open_items.csv", "manifest.json", "health_report.json"} <= \
        {path.name for path in (tmp_path / "out").iterdir()}
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["input_files"]["ground_truth"]["path"] == "ground_truth.csv"


def test_open_warnings_stop_the_run_until_they_are_acknowledged(tmp_path):
    code, printed = folder_run(SOCIAL, tmp_path / "out")

    assert code == GATE_CLOSED
    assert "--acknowledge" in printed and "amount_outlier:" in printed
    assert not (tmp_path / "out").exists()


def test_a_blocking_issue_stops_the_run_even_when_acknowledged(tmp_path):
    config = write_sources(tmp_path)
    mapping = {**MAPPING, "oms": {**MAPPING["oms"], "order_amount": "NoSuchColumn"}}
    (tmp_path / "config.json").write_text(replace(config, mapping=mapping).to_json(), encoding="utf-8")

    code, printed = folder_run(tmp_path, tmp_path / "out", note="seen")

    assert code == GATE_CLOSED and "Blocked" in printed


def test_a_folder_without_a_config_is_refused(tmp_path):
    assert main(["run", str(tmp_path)]) == NO_CONFIG


def test_the_sets_hold_no_labels_and_a_run_without_them_is_not_scored(tmp_path):
    assert not any((SOCIAL / name).exists() for name in LABEL_FILES)
    code, printed = folder_run(SOCIAL, tmp_path / "out", note="seen")

    assert code == 0 and "Accuracy" not in printed
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["metrics"]["accuracy"] is None and "ground_truth" not in manifest["input_files"]


def test_a_labels_folder_without_labels_is_refused(tmp_path):
    config = write_sources(tmp_path)
    (tmp_path / "labels").mkdir()
    with pytest.raises(FileNotFoundError, match="no labels"):
        run(tmp_path, acknowledged(tmp_path, config), labels=tmp_path / "labels")


def test_the_app_loads_a_local_folder_by_its_config(tmp_path):
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.switch_page("app_pages/load.py").run()
    at.segmented_control(key="load_mode").set_value("Local folder").run()
    at.text_input(key="local_folder").input(str(SOCIAL)).run()
    at.button(key="use_folder_config").click().run()
    assert not at.exception

    assert at.session_state["sources"] == str(SOCIAL)
    assert at.session_state["config"] == load_config(SOCIAL / "config.json")
