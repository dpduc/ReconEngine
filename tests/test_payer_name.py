"""Pass 2 does not link a credit from a different payer than the order's customer."""

from pathlib import Path

import pytest

from recon.acknowledgement import acknowledge_warnings
from recon.config import FeedSpec, Policy, RunConfig
from recon.evaluation import metrics
from recon.funnel.linking import normalize_name
from recon.pipeline import inspect, load_config, run

from test_funnel_order import BANK_HEADER, SHIP_HEADER, shipment

ROOT = Path(__file__).parents[1]
OMS_HEADER = "order_id,tracking_no,payment_method,order_amount,ordered_at,order_status,customer_name\n"
PATTERN = r"^[A-Z]+\.\d+\.(?P<payer>[A-Z ]+?) chuyen tien"


def transfer(ref: str, payer: str, amount: str = "263000") -> str:
    return f"{ref},MBVCB.1040223897.{payer} chuyen tien ck mua hang,{amount},0,{amount},2024-01-02\n"


def decide(folder: Path, customers: list[str], bank: list[str], bank_header: str = BANK_HEADER, **policy):
    oms = "".join(f"ORD2024-{n:07d},T{n},PREPAID,263000,2024-01-01T10:00:00,delivered,{name}\n"
                  for n, name in enumerate(customers, start=1))
    (folder / "oms.csv").write_text(OMS_HEADER + oms, encoding="utf-8")
    (folder / "bank.csv").write_text(bank_header + "".join(bank), encoding="utf-8")
    (folder / "ship.csv").write_text(SHIP_HEADER + "".join(shipment(n) for n in range(1, len(customers) + 1)),
                                     encoding="utf-8")
    config = RunConfig(
        feeds={"oms": FeedSpec("oms.csv"), "bank": FeedSpec("bank.csv"), "logistics": FeedSpec("ship.csv")},
        policy=Policy(**{"mdr_bps": 0, "cod_fee_bps": 0, "amount_tolerance": 1000, "payer_pattern": PATTERN,
                         **policy}))
    outcome = run(folder, acknowledge_warnings(config, inspect(folder, config), "test"))
    assert not outcome.blocked, [i.message for i in outcome.open_issues]
    return outcome.results.set_index("order_id")


def test_a_unique_match_from_a_different_payer_is_not_auto_accepted(tmp_path):
    unit = decide(tmp_path, ["Bùi Văn Hải"], [transfer("FT1", "TRAN THI MAI")]).loc["ORD2024-0000001"]

    assert (unit["decision"], unit["reason_codes"]) == ("REVIEW", "LINK_CANDIDATES")
    assert unit["link_candidates"].startswith("FT1:")


def test_names_that_agree_after_normalizing_still_link_in_pass_two(tmp_path):
    unit = decide(tmp_path, ["Bùi  Văn Hải"], [transfer("FT1", "BUI VAN HAI")]).loc["ORD2024-0000001"]

    assert (unit["decision"], unit["bank_link_pass"], unit["txn_ref"]) == ("AUTO_ACCEPT", "constrained", "FT1")


def test_a_credit_from_another_payer_does_not_make_the_customers_own_credit_ambiguous(tmp_path):
    unit = decide(tmp_path, ["Bùi Văn Hải"], [transfer("FT1", "TRAN THI MAI"),
                                              transfer("FT2", "BUI VAN HAI")]).loc["ORD2024-0000001"]

    assert (unit["decision"], unit["txn_ref"]) == ("AUTO_ACCEPT", "FT2")


def test_without_a_name_on_either_side_pass_two_links_as_before(tmp_path):
    no_payer = "FT1,TRANSFER RECEIVED,263000,0,263000,2024-01-02\n"
    assert decide(tmp_path, ["Bùi Văn Hải"], [no_payer]).loc["ORD2024-0000001", "bank_link_pass"] == "constrained"
    assert decide(tmp_path, [""], [transfer("FT1", "TRAN THI MAI")]).loc["ORD2024-0000001",
                                                                         "bank_link_pass"] == "constrained"
    assert decide(tmp_path, ["Bùi Văn Hải"], [transfer("FT1", "TRAN THI MAI")],
                  payer_pattern=None).loc["ORD2024-0000001", "bank_link_pass"] == "constrained"


def test_a_payer_name_column_is_used_instead_of_the_description(tmp_path):
    results = decide(tmp_path, ["Bùi Văn Hải"], ["FT1,BUI VAN HAI,263000,0,263000,2024-01-02,Tran Thi Mai\n"],
                     bank_header=BANK_HEADER.replace("\n", ",payer_name\n"), payer_pattern=None)

    assert results.loc["ORD2024-0000001", "decision"] == "REVIEW"


def test_a_payer_pattern_without_a_payer_group_is_rejected():
    with pytest.raises(ValueError, match="payer"):
        Policy(payer_pattern=r"^[A-Z]+\.\d+\.([A-Z ]+)")


def test_normalized_names_drop_diacritics_case_and_spacing():
    assert normalize_name("  Đặng   thị Ánh ") == "DANG THI ANH"
    assert normalize_name(None) == ""


def test_the_social_heavy_set_auto_accepts_no_wrong_payer():
    folder = ROOT / "test" / "vn" / "c_social_heavy"
    config = load_config(folder / "config.json")
    results = run(folder, acknowledge_warnings(config, inspect(folder, config), "test")).results

    assert metrics(results)["auto_accept_precision"] == 1.0
    accepted = results[results["decision"].eq("AUTO_ACCEPT")]
    assert not accepted["scenario"].eq("PAYER_NAME_DIFFERS").any()
