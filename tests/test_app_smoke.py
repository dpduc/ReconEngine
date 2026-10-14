"""Headless smoke checks of the wizard (Load -> Health -> Fix -> Run -> Review); the UI has no unit tests."""

import hashlib
import json
import tempfile
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from recon.config import RunConfig
from recon.pipeline import inspect

from test_ingest_formats import RENAMED_BANK, RENAMED_OMS, RENAMED_SHIP
from test_pipeline import write_sources

APP = str(Path(__file__).parents[1] / "app.py")
OLIST = Path(__file__).parents[1] / "data" / "generated"


def upload(at: AppTest, files: dict[str, tuple[str, bytes]]) -> AppTest:
    """Load page: upload one export per feed and confirm the suggested mapping."""
    at.switch_page("app_pages/load.py").run()
    for feed, (name, content) in files.items():
        at.file_uploader(key=f"upload_{feed}").set_value((name, content, "text/csv"))
    at.run()
    assert not at.exception
    at.button(key="confirm_mapping").click().run()
    assert not at.exception
    return at


def renamed(oms: str = RENAMED_OMS, bank: str = RENAMED_BANK) -> dict[str, tuple[str, bytes]]:
    return {"oms": ("orders.csv", oms.encode()), "bank": ("bank.csv", bank.encode()),
            "logistics": ("3pl.csv", RENAMED_SHIP.encode())}


def downloaded_config(at: AppTest) -> RunConfig:
    [shown] = [code for code in at.code if code.language == "json"]
    return RunConfig.from_dict(json.loads(shown.value))


def test_duplicate_key_upload_is_cleared_by_picking_a_strategy():
    duplicated = RENAMED_OMS + RENAMED_OMS.splitlines()[1] + "\n"
    at = upload(AppTest.from_file(APP, default_timeout=60).run(), renamed(oms=duplicated))

    at.switch_page("app_pages/health.py").run()
    assert not at.exception
    issue_id = "duplicate_key:oms:order_id"
    assert issue_id in [i.id for i in at.session_state["health"].blocking_issues]
    assert at.error  # gate closed

    at.switch_page("app_pages/fix.py").run()
    assert not at.exception
    at.selectbox(key=f"option:{issue_id}").select("Keep the first row per key").run()
    at.button(key=f"apply:{issue_id}").click().run()
    assert not at.exception
    health = at.session_state["health"]
    assert issue_id not in [i.id for i in health.issues]
    assert health.gate_open
    assert at.success

    # The run config offered for download reproduces this Health Report through inspect.
    config = downloaded_config(at)
    assert config == at.session_state["config"]
    assert inspect(at.session_state["sources"], config).to_json() == health.to_json()

    at.switch_page("app_pages/run.py").run()
    assert not at.button(key="run").disabled


def test_flagged_mapping_is_remapped_on_the_fix_page():
    bank = RENAMED_BANK.replace("Credit,Charges,Net Settled", "Amt1,Amt2,Amt3")
    at = upload(AppTest.from_file(APP, default_timeout=60).run(), renamed(bank=bank))

    at.switch_page("app_pages/fix.py").run()
    for canonical, source in (("credit_amount", "Amt1"), ("fee_amount", "Amt2"), ("net_amount", "Amt3")):
        issue_id = f"missing_column:bank:{canonical}"
        at.selectbox(key=f"remap:{issue_id}").select(source).run()
        at.button(key=f"apply:{issue_id}").click().run()
        assert not at.exception
    remaps = {r.params["canonical"]: r.params["source"] for r in at.session_state["config"].resolutions
              if r.option == "remap"}
    assert remaps == {"credit_amount": "Amt1", "fee_amount": "Amt2", "net_amount": "Amt3"}
    assert at.session_state["health"].gate_open


@pytest.mark.skipif(not (OLIST / "oms_orders.csv").exists(), reason="run recon.olistdata first")
def test_benchmark_upload_opens_the_gate_after_acknowledging_warnings():
    files = {feed: (name, (OLIST / name).read_bytes()) for feed, name in
             {"oms": "oms_orders.csv", "bank": "bank_statement.csv", "logistics": "logistics_report.csv"}.items()}
    at = upload(AppTest.from_file(APP, default_timeout=600).run(), files)

    at.switch_page("app_pages/fix.py").run()
    assert not at.exception
    health = at.session_state["health"]
    assert not health.blocking_issues and not health.gate_open  # only warnings stand in the way

    at.text_input(key="acknowledge_all_note").input("Known Olist data gaps").run()
    at.button(key="acknowledge_all").click().run()
    assert not at.exception
    assert at.session_state["health"].gate_open


def test_run_then_review_without_exceptions(tmp_path):
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["sources"] = str(tmp_path)
    at.session_state["config"] = write_sources(tmp_path)
    at.run()
    at.switch_page("app_pages/run.py").run()
    assert not at.exception
    assert at.info  # custom inputs wait for an explicit Run

    at.button(key="run").click().run()
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


def test_uploads_stay_in_memory():
    temp = Path(tempfile.gettempdir())
    before = set(temp.glob("recon-upload-*"))

    at = upload(AppTest.from_file(APP, default_timeout=60).run(), renamed())

    assert set(temp.glob("recon-upload-*")) == before
    sources = at.session_state["sources"]
    assert isinstance(sources, dict) and sorted(sources) == ["bank/bank.csv", "logistics/3pl.csv", "oms/orders.csv"]


def test_local_folder_is_read_in_place(tmp_path):
    for feed, (name, content) in renamed().items():
        (tmp_path / name).write_bytes(content)
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.switch_page("app_pages/load.py").run()
    at.segmented_control(key="load_mode").set_value("Local folder").run()
    at.text_input(key="local_folder").input(str(tmp_path)).run()
    for feed, (name, _) in renamed().items():
        at.selectbox(key=f"folder_{feed}").set_value(name)
    at.run()
    assert not at.exception
    at.button(key="confirm_mapping").click().run()
    assert not at.exception

    assert at.session_state["sources"] == str(tmp_path)
    assert at.session_state["config"].feeds["bank"].path == "bank.csv"
    assert inspect(str(tmp_path), at.session_state["config"]).gate_open


def test_reruns_do_not_rehash_uploaded_files(monkeypatch):
    """Identifying in-memory sources must not re-hash their bytes on every rerun (slow on big exports)."""
    at = upload(AppTest.from_file(APP, default_timeout=60).run(), renamed())
    at.switch_page("app_pages/health.py").run()
    at.switch_page("app_pages/fix.py").run()
    content = {content for _, content in renamed().values()}
    hashed = []
    real_sha256 = hashlib.sha256

    def counting_sha256(data=b"", **kwargs):
        if data in content:
            hashed.append(len(data))
        return real_sha256(data, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", counting_sha256)
    for page in ("app_pages/health.py", "app_pages/fix.py", "app_pages/load.py", "app_pages/run.py"):
        at.switch_page(page).run()
        assert not at.exception
    assert hashed == []
