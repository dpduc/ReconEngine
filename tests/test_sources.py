"""In-memory sources: the same files behave identically whether read from a folder or from bytes."""

from pathlib import Path

import pandas as pd
import pytest

from recon.pipeline import draft_config, inspect, run

from test_ingest_formats import RENAMED_FILES, _write_renamed
from test_pipeline import write_sources


def _in_memory(folder: Path) -> dict[str, bytes]:
    return {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob("*") if path.is_file()}


def test_folder_and_in_memory_sources_give_identical_runs(tmp_path):
    config = write_sources(tmp_path)
    memory = _in_memory(tmp_path)

    assert inspect(memory, config).to_json() == inspect(tmp_path, config).to_json()
    from_folder, from_memory = run(tmp_path, config), run(memory, config)
    assert from_memory.health.gate_open
    lineage = ["oms_ingested_at", "bank_ingested_at", "logistics_ingested_at"]
    pd.testing.assert_frame_equal(from_memory.results.drop(columns=lineage, errors="ignore"),
                                  from_folder.results.drop(columns=lineage, errors="ignore"))
    pd.testing.assert_frame_equal(from_memory.funnel, from_folder.funnel)
    assert from_memory.manifest["input_files"] == from_folder.manifest["input_files"]


def test_draft_config_reads_an_in_memory_source_and_detects_its_dialect(tmp_path):
    _write_renamed(tmp_path)
    bank = tmp_path / RENAMED_FILES["bank"]
    bank.write_bytes(bank.read_text(encoding="utf-8").replace(",", ";").encode("cp1252"))
    memory = _in_memory(tmp_path)

    draft = draft_config(memory, RENAMED_FILES)

    assert draft.config.feeds["bank"].delimiter == ";"
    assert draft.flagged == []
    assert draft.config.mapping == draft_config(tmp_path, RENAMED_FILES).config.mapping
    assert inspect(memory, draft.config).gate_open


def test_in_memory_source_missing_a_configured_file_names_it(tmp_path):
    config = write_sources(tmp_path)
    memory = _in_memory(tmp_path)
    del memory[config.feeds["bank"].path]

    with pytest.raises(FileNotFoundError, match=config.feeds["bank"].path):
        inspect(memory, config)
