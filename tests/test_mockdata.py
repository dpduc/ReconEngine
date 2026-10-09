import csv
from collections import Counter
from datetime import date, timedelta

import pytest

from recon.contracts import AMOUNT_TOLERANCE, COD_FEE_BPS, MDR_BPS, WINDOW_DAYS, fee
from recon.mockdata import Generator, allocate, generate

START, END = date(2026, 9, 1), date(2026, 9, 30)
ROWS = 5_000


@pytest.fixture(scope="module")
def units():
    return Generator(ROWS, seed=7, period_start=START, period_end=END).build()


def by_kind(units, sub_kind):
    found = [u for u in units if u.sub_kind == sub_kind]
    assert found, f"no {sub_kind} units generated"
    return found


def leg_dates(u):
    dates = []
    if u.order:
        dates.append(u.order.ordered_at.date())
    if u.bank:
        dates.append(u.bank.value_date)
    if u.shipment:
        dates.append(u.shipment.delivered_at.date())
    return dates


def in_period(d):
    return START <= d <= END


def three_way_consistent(u):
    """Amounts tie out across all legs under contract terms."""
    o, b, s = u.order, u.bank, u.shipment
    if o.payment_method == "PREPAID":
        return (b.gross == o.amount and b.fee == fee(b.gross, MDR_BPS)
                and b.net == b.gross - b.fee and s.cod == 0)
    return (s.cod == o.amount and s.cod_fee == fee(s.cod, COD_FEE_BPS)
            and b.gross == s.cod - s.ship_fee - s.cod_fee)


def test_allocate_is_exact():
    counts = allocate(99_999, {"a": 0.9, "b": 0.05, "c": 0.03, "d": 0.02})
    assert sum(counts.values()) == 99_999
    assert counts["a"] == 89_999


def test_scenario_mix(units):
    counts = Counter(u.scenario for u in units)
    assert counts == {"MATCHED": 4_500, "CUTOFF_LAG": 250, "AMOUNT_ERROR": 150, "ORPHAN": 100}


def test_matched_units_tie_out_inside_period(units):
    for u in by_kind(units, "MATCHED"):
        assert u.order and u.bank and u.shipment
        assert u.shipment.order_ref == u.order.order_id
        assert u.shipment.tracking_no == u.order.tracking_no
        assert three_way_consistent(u)
        assert all(in_period(d) for d in leg_dates(u))


def test_cutoff_units_cross_period_within_window(units):
    for kind in ("LATE_SETTLEMENT", "EARLY_ORDER"):
        for u in by_kind(units, kind):
            assert three_way_consistent(u)
            dates = leg_dates(u)
            assert not all(in_period(d) for d in dates)
            assert all(START - timedelta(WINDOW_DAYS) <= d <= END + timedelta(WINDOW_DAYS)
                       for d in dates)


def test_rounding_is_within_tolerance(units):
    for u in by_kind(units, "ROUNDING"):
        assert 0 < abs(u.bank.gross - u.order.amount) <= AMOUNT_TOLERANCE


def test_cod_short_exceeds_tolerance(units):
    for u in by_kind(units, "COD_SHORT"):
        assert u.order.amount - u.shipment.cod > AMOUNT_TOLERANCE
        assert u.bank.gross == u.shipment.cod - u.shipment.ship_fee - u.shipment.cod_fee


def test_fee_overcharges_deviate_from_contract(units):
    for u in by_kind(units, "MDR_OVERCHARGE"):
        assert u.bank.fee - fee(u.bank.gross, MDR_BPS) >= AMOUNT_TOLERANCE
        assert u.bank.gross == u.order.amount
    for u in by_kind(units, "COD_FEE_OVERCHARGE"):
        assert u.shipment.cod_fee - fee(u.shipment.cod, COD_FEE_BPS) >= AMOUNT_TOLERANCE
        assert u.shipment.cod == u.order.amount


def test_orphans_have_exactly_the_expected_legs(units):
    order_ids = {u.order.order_id for u in units if u.order}
    for u in by_kind(units, "PHANTOM_PAYMENT"):
        assert (u.order, u.shipment) == (None, None)
        assert not any(oid in u.bank.description for oid in order_ids)
    for u in by_kind(units, "PHANTOM_SHIPMENT"):
        assert (u.order, u.bank) == (None, None)
        assert u.shipment.order_ref not in order_ids
    for u in by_kind(units, "MISSING_PAYMENT"):
        assert u.order and u.shipment and u.bank is None
    for u in by_kind(units, "MISSING_SHIPMENT"):
        assert u.order and u.bank and u.shipment is None


def test_all_credits_are_positive(units):
    assert all(u.bank.gross > 0 and u.bank.net > 0 for u in units if u.bank)


def test_timelines_are_causal(units):
    for u in units:
        s, b = u.shipment, u.bank
        if u.order and s:
            assert u.order.ordered_at < s.picked_at < s.delivered_at
        if s and b and s.remittance_ref:
            assert s.delivered_at <= b.txn_at
            assert b.txn_at.date() == b.value_date == s.delivered_at.date()


def test_output_is_deterministic_and_has_duplicates(tmp_path):
    a = generate(1_000, 42, START, END, tmp_path / "a")
    b = generate(1_000, 42, START, END, tmp_path / "b")
    c = generate(1_000, 43, START, END, tmp_path / "c")
    sha = lambda m: {k: v["sha256"] for k, v in m["files"].items()}
    assert sha(a) == sha(b)
    assert sha(a) != sha(c)

    with (tmp_path / "a" / "bank_statement.csv").open(encoding="utf-8") as f:
        lines = list(csv.reader(f))[1:]
    distinct = {tuple(r) for r in lines}
    assert len(lines) - len(distinct) == a["files"]["bank_statement.csv"]["duplicate_rows"] > 0
    assert len({r[0] for r in lines}) == len(distinct)  # txn_ref unique among distinct rows
