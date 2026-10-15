"""Fee rate per bank payment type: free transfers, gateway rates, and the fallback MDR."""

from pathlib import Path

from recon.acknowledgement import acknowledge_warnings
from recon.config import FeedSpec, Policy, RunConfig
from recon.pipeline import inspect, load_config, run

from test_funnel_order import OMS_HEADER, SHIP_HEADER, by_order, order, shipment

ROOT = Path(__file__).parents[1]
BANK_HEADER = "txn_ref,description,credit_amount,fee_amount,net_amount,value_date,payment_type\n"
RATES = {"bank_transfer": 0, "wallet": 110}


def credit(n: int, payment_type: str, fee: str) -> str:
    return f"P{n},PAYMENT ORD2024-{n:07d},100000,{fee},{100000 - int(fee)},2024-01-02,{payment_type}\n"


def config_for(folder: Path, bank: list[str], **policy) -> RunConfig:
    count = len(bank)
    (folder / "oms.csv").write_text(OMS_HEADER + "".join(order(n, amount="100000") for n in range(1, count + 1)),
                                    encoding="utf-8")
    (folder / "bank.csv").write_text(BANK_HEADER + "".join(bank), encoding="utf-8")
    (folder / "ship.csv").write_text(SHIP_HEADER + "".join(shipment(n) for n in range(1, count + 1)),
                                     encoding="utf-8")
    return RunConfig(
        feeds={"oms": FeedSpec("oms.csv"), "bank": FeedSpec("bank.csv"), "logistics": FeedSpec("ship.csv")},
        policy=Policy(**{"mdr_bps": 110, "cod_fee_bps": 0, "amount_tolerance": 0.01, **policy}))


def decide(folder: Path, bank: list[str], **policy):
    config = config_for(folder, bank, **policy)
    outcome = run(folder, acknowledge_warnings(config, inspect(folder, config), "test"))
    assert not outcome.blocked, [i.message for i in outcome.open_issues]
    return outcome


def test_a_fee_free_transfer_is_not_a_fee_mismatch_and_a_wallet_credit_is_checked_at_its_rate(tmp_path):
    outcome = decide(tmp_path, [credit(1, "bank_transfer", "0"), credit(2, "wallet", "1100"),
                                credit(3, "wallet", "0")], payment_type_fee_bps=RATES)

    decisions = by_order(outcome)
    assert decisions[1][:2] == ("AUTO_ACCEPT", "THREE_WAY_TIE_OUT")
    assert decisions[2][:2] == ("AUTO_ACCEPT", "THREE_WAY_TIE_OUT")
    assert decisions[3][:2] == ("EXCEPTION", "FEE_MISMATCH")
    explanation = outcome.results.set_index("order_id").loc["ORD2024-0000003", "explanation"]
    assert explanation.startswith("The bank fee is 1,100.00 less than the contract fee (1.10%).")


def test_without_rates_per_payment_type_every_prepaid_credit_is_checked_at_the_mdr(tmp_path):
    decisions = by_order(decide(tmp_path, [credit(1, "bank_transfer", "0"), credit(2, "wallet", "1100")]))

    assert decisions[1][:2] == ("EXCEPTION", "FEE_MISMATCH")
    assert decisions[2][:2] == ("AUTO_ACCEPT", "THREE_WAY_TIE_OUT")


def test_a_payment_type_without_a_rate_is_a_warning_and_falls_back_to_the_mdr(tmp_path):
    bank = [credit(1, "card", "1100"), credit(2, "bank_transfer", "0"), credit(3, "", "1100")]
    report = inspect(tmp_path, config_for(tmp_path, bank, payment_type_fee_bps=RATES))

    warned = [issue for issue in report.issues if issue.check == "payment_type_without_fee_rate"]
    assert [(issue.id, issue.affected_rows) for issue in warned] == [("payment_type_without_fee_rate:card", 1)]
    assert "110 bps" in warned[0].message
    assert all(by_order(decide(tmp_path, bank, payment_type_fee_bps=RATES))[n][0] == "AUTO_ACCEPT" for n in (1, 2, 3))


def test_no_fee_mismatch_in_the_vietnamese_sets_comes_from_a_bank_transfer():
    for folder in sorted((ROOT / "test" / "vn").iterdir()):
        config = load_config(folder / "config.json")
        results = run(folder, acknowledge_warnings(config, inspect(folder, config), "test")).results
        fee = results[results["reason_codes"].str.split("|").map(lambda codes: "FEE_MISMATCH" in codes)]
        assert not fee["payment_type_bank"].eq("bank_transfer").any(), folder.name
        assert fee["truth_exception"].eq("FEE_MISMATCH").all(), folder.name
