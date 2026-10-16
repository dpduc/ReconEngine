"""COD remittances: order -> shipment -> carrier remittance -> bank credit, with a shipment check and a batch tie-out."""

from pathlib import Path

import pandas as pd

from recon.acknowledgement import acknowledge_warnings
from recon.channeldata import labels_for
from recon.config import Channel, FeedSpec, Policy, RunConfig
from recon.evaluation import link_metrics
from recon.pipeline import inspect, load_config, run

from test_funnel_order import BANK_HEADER
from test_pipeline import acknowledged

ROOT = Path(__file__).parents[1]
OMS_HEADER = "order_id,tracking_no,payment_method,order_amount,ordered_at,order_status,channel\n"
SHIP_HEADER = "tracking_code,client_order_code,carrier,status,cod_collected,ship_fee,cod_fee,remittance_id,picked_at,delivered_at\n"
FEEDS = {"oms": FeedSpec("oms.csv"), "bank": FeedSpec("bank.csv"), "logistics": FeedSpec("ship.csv")}
POLICY = Policy(mdr_bps=0, cod_fee_bps=100, amount_tolerance=0.01)


def oid(n: int) -> str:
    return f"ORD2024-{n:07d}"


def order(n: int, method: str = "COD", amount: int = 100) -> str:
    return f"{oid(n)},T{n},{method},{amount},2024-01-01T10:00:00,delivered,website\n"


def shipment(n: int, cod: int = 100, ship_fee: int = 5, cod_fee: int = 1, remittance: str = "GHN-1",
             status: str = "DELIVERED") -> str:
    return f"T{n},{oid(n)},GHN,{status},{cod},{ship_fee},{cod_fee},{remittance},2024-01-02T08:00:00,2024-01-03T08:00:00\n"


def remittance(amount: int, remittance_id: str = "GHN-1", ref: str = "R1") -> str:
    return f"{ref},GHN CHUYEN TIEN COD PHIEN {remittance_id},{amount},0,{amount},2024-01-05\n"


# Two COD orders and a prepaid one, all three parcels on remittance GHN-1: 94 + 94 - 5 = 183.
ORDERS = [order(1), order(2), order(3, method="PREPAID", amount=50)]
SHIPMENTS = [shipment(1), shipment(2), shipment(3, cod=0, cod_fee=0)]
TRANSFER = f"B3,CK THANH TOAN {oid(3)},50,0,50,2024-01-01\n"


def settle(folder: Path, shipments: list[str], bank: list[str], orders: list[str] = ORDERS,
           channels: dict[str, Channel] | None = None):
    (folder / "oms.csv").write_text(OMS_HEADER + "".join(orders), encoding="utf-8")
    (folder / "bank.csv").write_text(BANK_HEADER + "".join(bank), encoding="utf-8")
    (folder / "ship.csv").write_text(SHIP_HEADER + "".join(shipments), encoding="utf-8")
    config = RunConfig(feeds=FEEDS, policy=POLICY,
                       channels={"website": Channel(cod_remittances=True)} if channels is None else channels)
    outcome = run(folder, acknowledged(folder, config))
    assert not outcome.blocked, [i.message for i in outcome.open_issues]
    return outcome


def decisions(outcome) -> dict[str, tuple[str, str]]:
    return {row.order_id: (row.decision, row.reason_codes) for row in outcome.results.itertuples()}


def batch(outcome, remittance_id: str = "GHN-1") -> pd.Series:
    return outcome.settlement_batches.set_index("payout_id").loc[remittance_id]


def explained(outcome, n: int) -> str:
    return outcome.results.set_index("order_id").loc[oid(n), "explanation"]


def test_cod_orders_on_a_remittance_that_ties_out_are_auto_accepted(tmp_path):
    outcome = settle(tmp_path, SHIPMENTS, [remittance(183), TRANSFER])

    assert decisions(outcome) == {oid(1): ("AUTO_ACCEPT", "REMITTANCE_TIE_OUT"),
                                  oid(2): ("AUTO_ACCEPT", "REMITTANCE_TIE_OUT"),
                                  oid(3): ("AUTO_ACCEPT", "THREE_WAY_TIE_OUT")}
    units = outcome.results.set_index("order_id")
    assert list(units.loc[[oid(1), oid(2)], "bank_link_pass"]) == ["remittance"] * 2
    assert list(units["remittance_txn_ref"]) == ["R1"] * 3  # the prepaid parcel's shipping is netted there too
    assert outcome.orphans.empty
    paid = batch(outcome)
    assert (paid["settlement_feed"], paid["channel"], paid["lines"], paid["orders"], paid["net"],
            paid["reason_codes"]) == ("logistics", "GHN", 3, 2, 183, "BATCH_TIE_OUT")
    assert paid["explanation"] == "Bank credit R1 equals the net of remittance GHN-1's 3 lines."


def test_cod_short_is_an_amount_mismatch_on_its_order_and_the_remittance_still_ties_out(tmp_path):
    shipments = [shipment(1), shipment(2, cod=80), SHIPMENTS[2]]
    outcome = settle(tmp_path, shipments, [remittance(163), TRANSFER])

    assert decisions(outcome)[oid(2)] == ("EXCEPTION", "AMOUNT_MISMATCH")
    assert explained(outcome, 2).startswith("The COD collected is 20.00 less than the order amount.")
    assert (batch(outcome)["reason_codes"], batch(outcome)["exception_orders"]) == ("BATCH_TIE_OUT", oid(2))


def test_a_cod_fee_off_the_carriers_terms_is_a_cod_fee_mismatch(tmp_path):
    shipments = [shipment(1), shipment(2, cod_fee=6), SHIPMENTS[2]]
    outcome = settle(tmp_path, shipments, [remittance(178), TRANSFER])

    assert decisions(outcome)[oid(2)] == ("EXCEPTION", "COD_FEE_MISMATCH")
    assert decisions(outcome)[oid(1)] == ("AUTO_ACCEPT", "REMITTANCE_TIE_OUT")


def test_a_returned_shipment_not_in_the_oms_is_reported_as_missing_payment(tmp_path):
    shipments = [shipment(1), shipment(2, cod=0, ship_fee=8, cod_fee=0, status="RETURNED"), SHIPMENTS[2]]
    outcome = settle(tmp_path, shipments, [remittance(81), TRANSFER])

    assert decisions(outcome)[oid(2)] == ("EXCEPTION", "MISSING_PAYMENT")
    assert explained(outcome, 2).startswith("Shipment T2 was returned to the seller: the carrier collected no COD")
    assert batch(outcome)["reason_codes"] == "BATCH_TIE_OUT"


def test_a_remittance_credit_off_its_shipments_puts_its_cod_orders_in_review(tmp_path):
    outcome = settle(tmp_path, SHIPMENTS, [remittance(150), TRANSFER])

    assert (batch(outcome)["reason_codes"], batch(outcome)["tie_out_delta"]) == ("SETTLEMENT_BATCH_MISMATCH", -33)
    assert decisions(outcome) == {oid(1): ("REVIEW", "SETTLEMENT_BATCH_MISMATCH"),
                                  oid(2): ("REVIEW", "SETTLEMENT_BATCH_MISMATCH"),
                                  oid(3): ("AUTO_ACCEPT", "THREE_WAY_TIE_OUT")}
    assert explained(outcome, 1).startswith("The bank credit for remittance GHN-1 is 33.00 less than the net of its "
                                            "lines; every order in the batch is reviewed.")


def test_cod_orders_without_a_shipment_a_remittance_or_a_credit_are_exceptions(tmp_path):
    orders = [order(1), order(2), order(4)]
    outcome = settle(tmp_path, [shipment(1), shipment(2, remittance="")], [], orders=orders)

    assert decisions(outcome) == {oid(1): ("EXCEPTION", "MISSING_PAYMENT"), oid(2): ("EXCEPTION", "MISSING_PAYMENT"),
                                  oid(4): ("EXCEPTION", "MISSING_SHIPMENT")}
    assert explained(outcome, 1).startswith("Remittance GHN-1 pays shipment T1, but no bank credit names it.")
    assert explained(outcome, 2).startswith("The carrier has not put shipment T2 on a remittance yet.")
    assert batch(outcome)["reason_codes"] == "MISSING_PAYMENT"


def test_without_remittance_channels_cod_orders_take_the_per_order_path(tmp_path):
    outcome = settle(tmp_path, SHIPMENTS, [remittance(183), TRANSFER], channels={})

    assert outcome.funnel.set_index("step").loc["settle_cod", "decided"] == 0
    assert outcome.settlement_batches.empty
    assert "remittance" not in set(outcome.results["bank_link_pass"].dropna())


def test_link_metrics_score_the_remittance_links(tmp_path):
    outcome = settle(tmp_path, SHIPMENTS, [remittance(183), TRANSFER])
    truth = pd.DataFrame({"feed": ["bank"] * 4, "record_key": ["R1", "R1", "R1", "B3"],
                          "order_id": [oid(1), oid(2), oid(3), oid(3)],
                          "link_kind": ["cod_remittance"] * 3 + ["order_ref"]})

    links = link_metrics(outcome.results, truth)

    assert links["per_link_kind"]["bank:cod_remittance"] == {"true_links": 3, "found": 3, "recall": 1.0}
    assert links["per_pass"]["bank:remittance"] == {"links": 3, "correct": 3, "precision": 1.0}


def test_a_channels_cod_remittances_survive_the_config_round_trip():
    config = RunConfig(feeds=FEEDS, channels={"website": Channel(cod_remittances=True), "shopee": Channel()})

    assert RunConfig.from_dict(config.to_dict()) == config
    assert config.cod_channels == ("website",)


def test_the_social_set_auto_accepts_every_cod_order_on_a_correct_remittance():
    folder = ROOT / "test" / "vn" / "c_social_heavy"
    config = load_config(folder / "config.json")
    outcome = run(folder, acknowledge_warnings(config, inspect(folder, config), "test"), labels=labels_for(folder))
    cod = outcome.results[outcome.results["payment_method"].eq("COD")]
    codes = cod.groupby("scenario")["reason_codes"].agg(set).to_dict()

    assert codes["MATCHED"] == {"REMITTANCE_TIE_OUT"}
    assert codes["COD_SHORT"] == {"AMOUNT_MISMATCH"}
    assert codes["COD_RETURNED_NOT_IN_OMS"] == {"MISSING_PAYMENT"}
    assert codes["COD_FEE_OVERCHARGE"] == {"COD_FEE_MISMATCH"}
    remittances = outcome.settlement_batches[outcome.settlement_batches["settlement_feed"].eq("logistics")]
    assert set(remittances["reason_codes"]) == {"BATCH_TIE_OUT"}
    assert outcome.manifest["metrics"]["links"]["per_link_kind"]["bank:cod_remittance"]["recall"] == 1.0
