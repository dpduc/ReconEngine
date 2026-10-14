"""Behaviour of the gated pipeline through its public seam: inspect / run."""

import hashlib
from pathlib import Path

import pandas as pd

from recon.acknowledgement import acknowledge_warnings
from recon.config import FeedSpec, Policy, Resolution, RunConfig
from recon.pipeline import inspect, run


# Exports use the source systems' own file and column names; the mapping
# renames them into the canonical schema.
OMS = """OrderNo,Tracking,Method,Amount,Placed,Status
ORD2024-0000001,T1,PREPAID,100.00,2024-01-01T10:00:00,delivered
ORD2024-0000002,T2,PREPAID,50.00,2024-01-02T10:00:00,delivered
ORD2024-0000003,T3,PREPAID,70.00,2024-01-03T10:00:00,delivered
"""
BANK = """Ref,Memo,Gross,Fee,Net,ValueDate
P1,PAYMENT ORD2024-0000001,100.00,0,100.00,2024-01-02
P2,PAYMENT ORD2024-0000002,55.00,0,55.00,2024-01-03
"""
SHIP = """Code,OrderRef,COD,ShipFee,CODFee
T1,ORD2024-0000001,0,5.0,0
T2,ORD2024-0000002,0,5.0,0
T3,ORD2024-0000003,0,5.0,0
"""
MAPPING = {
    "oms": {"order_id": "OrderNo", "tracking_no": "Tracking", "payment_method": "Method",
            "order_amount": "Amount", "ordered_at": "Placed"},
    "bank": {"txn_ref": "Ref", "description": "Memo", "credit_amount": "Gross",
             "fee_amount": "Fee", "net_amount": "Net", "value_date": "ValueDate"},
    "logistics": {"tracking_code": "Code", "client_order_code": "OrderRef",
                  "cod_collected": "COD", "ship_fee": "ShipFee", "cod_fee": "CODFee"},
}
POLICY = Policy(mdr_bps=0, cod_fee_bps=0, amount_tolerance=0.01)
# ORD2024-0000003 is unpaid on purpose; acknowledge that warning so the gate opens.
FIXTURE_ACKNOWLEDGEMENTS = (
    Resolution("coverage:oms:no_payment", "acknowledge", note="ORD2024-0000003 is the MISSING_PAYMENT case"),
)


def write_sources(folder: Path, oms: str = OMS, bank: str = BANK, ship: str = SHIP) -> RunConfig:
    (folder / "orders export (jan).csv").write_text(oms, encoding="utf-8")
    (folder / "stmt_2024_01.csv").write_text(bank, encoding="utf-8")
    (folder / "3pl.csv").write_text(ship, encoding="utf-8")
    return RunConfig(
        feeds={
            "oms": FeedSpec("orders export (jan).csv"),
            "bank": FeedSpec("stmt_2024_01.csv"),
            "logistics": FeedSpec("3pl.csv"),
        },
        mapping=MAPPING,
        policy=POLICY,
        resolutions=FIXTURE_ACKNOWLEDGEMENTS,
    )


def acknowledged(folder: Path, config: RunConfig) -> RunConfig:
    """`config` with every warning acknowledged, for fixtures that are dirty on purpose."""
    return acknowledge_warnings(config, inspect(folder, config), note="Deliberate gap in this test fixture")


def test_clean_sources_have_no_blocking_issues(tmp_path):
    config = write_sources(tmp_path)
    report = inspect(tmp_path, config)
    assert report.blocking_issues == []
    assert report.gate_open


def test_missing_required_column_blocks_run(tmp_path):
    config = write_sources(tmp_path, oms=OMS.replace("Amount", "Total"))
    report = inspect(tmp_path, config)
    [issue] = report.blocking_issues
    assert issue.severity.value == "blocking"
    assert issue.feed == "oms"
    assert "order_amount" in issue.message
    assert not report.gate_open

    outcome = run(tmp_path, config)
    assert outcome.blocked
    assert [i.id for i in outcome.open_issues] == [issue.id]
    assert outcome.results.empty


def test_run_decides_every_unit_with_lineage(tmp_path):
    config = write_sources(tmp_path)
    outcome = run(tmp_path, config)
    assert not outcome.blocked
    results = outcome.results.set_index("order_id")

    assert results.loc["ORD2024-0000001", "decision"] == "AUTO_ACCEPT"
    assert results.loc["ORD2024-0000002", "decision"] == "EXCEPTION"
    assert "AMOUNT_MISMATCH" in results.loc["ORD2024-0000002", "reason_codes"]
    assert results.loc["ORD2024-0000002", "credit_delta"] == 5.0
    assert results.loc["ORD2024-0000003", "reason_codes"] == "MISSING_PAYMENT"

    assert results["deciding_step"].notna().all()
    assert results["rule_version"].notna().all()
    # A fully linked unit traces back to one row in each of the three feeds.
    assert len(results.loc["ORD2024-0000001", "input_row_hashes"].split("|")) == 3
    assert len(results.loc["ORD2024-0000003", "input_row_hashes"].split("|")) == 2
    assert outcome.funnel["decided"].sum() == len(results) == 3


def test_manifest_hashes_inputs_and_raw_files_are_untouched(tmp_path):
    config = write_sources(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    outcome = run(tmp_path, config)
    after = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert after == before

    manifest = outcome.manifest
    assert manifest["input_files"]["bank"]["sha256"] == hashlib.sha256(before["stmt_2024_01.csv"]).hexdigest()
    assert manifest["input_files"]["oms"]["path"] == "orders export (jan).csv"
    assert RunConfig.from_dict(manifest["run_config"]) == config
    assert manifest["rule_version"] == outcome.results["rule_version"].iloc[0]
    assert manifest["created_at"]
    assert manifest["metrics"]["rows"] == 3


def test_same_sources_and_config_give_identical_runs(tmp_path):
    config = write_sources(tmp_path)
    first, second = run(tmp_path, config), run(tmp_path, config)
    pd.testing.assert_frame_equal(first.results, second.results)
    pd.testing.assert_frame_equal(first.funnel, second.funnel)
    # When it ran and how long it took differ by nature; everything else must match.
    strip = lambda m: {k: v for k, v in m.items() if k not in {"created_at", "performance"}}
    assert strip(first.manifest) == strip(second.manifest)


def test_remap_resolution_maps_a_source_column_and_undo_removes_it(tmp_path):
    from recon.pipeline import resolve, undo

    config = write_sources(tmp_path, oms=OMS.replace("Amount", "Total"))
    [issue] = inspect(tmp_path, config).blocking_issues
    [remap] = issue.fix_options
    assert "Total" in remap.params["candidates"]

    resolved = resolve(config, issue.id, remap.id, {**remap.params, "source": "Total"})
    assert inspect(tmp_path, resolved).gate_open
    outcome = run(tmp_path, resolved)
    assert outcome.results.set_index("order_id").loc["ORD2024-0000001", "decision"] == "AUTO_ACCEPT"

    assert undo(resolved, len(resolved.resolutions) - 1) == config
