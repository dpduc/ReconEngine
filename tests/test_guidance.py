"""Every Health issue explains itself and recommends what to pick, through inspect."""

import json
from dataclasses import replace

from recon.pipeline import ACKNOWLEDGE, DUPLICATE_STRATEGY, SET_STATUS_POLICY, guide, inspect

from test_currency import MISLABELLED_BANK
from test_currency import OMS as CURRENCY_OMS
from test_currency import SHIP as CURRENCY_SHIP
from test_pipeline import BANK, MAPPING, OMS, SHIP, write_sources

# Dirty on purpose: a duplicate order, uncovered statuses, an unpaid order, a bad date.
DIRTY_OMS = OMS.replace("2024-01-03T10:00:00,delivered", "2024-01-03T10:00:00,canceled") + (
    "ORD2024-0000001,T1,PREPAID,100.00,2024-01-05T10:00:00,delivered\n"
    "ORD2024-0000004,T4,PREPAID,10.00,2024-01-06T10:00:00,invoiced\n"
    "ORD2024-0000005,T5,PREPAID,10.00,2024-01-06T10:00:00,on_hold_by_robot\n"
)
DIRTY_BANK = BANK.replace("2024-01-03\n", "03/01/2024\n")


def with_status(config):
    """`config` that also maps the OMS Status column, so the status policy check runs."""
    return replace(config, mapping={**config.mapping, "oms": {**config.mapping["oms"], "order_status": "Status"}})


def issues_by_id(report):
    return {issue.id: issue for issue in report.issues}


def recommended(issue):
    advice = guide(issue)
    assert advice.recommended is not None, issue.id
    return issue.fix_options[advice.recommended]


def all_issues(tmp_path):
    """Issues from three fixtures that together trigger every kind of check."""
    for name in ("dirty", "cols", "ccy"):
        (tmp_path / name).mkdir()
    dirty = with_status(write_sources(tmp_path / "dirty", oms=DIRTY_OMS, bank=DIRTY_BANK))
    bank_without_credit = {k: v for k, v in MAPPING["bank"].items() if k != "credit_amount"}
    unmapped = replace(write_sources(tmp_path / "cols"), mapping={**MAPPING, "bank": bank_without_credit})
    currency = write_sources(tmp_path / "ccy", oms=CURRENCY_OMS, bank=MISLABELLED_BANK, ship=CURRENCY_SHIP)
    return [*inspect(tmp_path / "dirty", dirty).issues, *inspect(tmp_path / "cols", unmapped).issues,
            *inspect(tmp_path / "ccy", currency).issues]


def test_every_issue_has_specific_guidance_with_a_valid_recommendation(tmp_path):
    issues = all_issues(tmp_path)
    checks = {issue.check for issue in issues}
    assert {"duplicate_key", "status_not_in_policy", "coverage", "unparseable_date",
            "missing_required_column", "currency_mismatch"} <= checks
    for issue in issues:
        advice = guide(issue)
        assert advice.meaning and advice.next_step, issue.id
        assert not advice.generic, issue.id
        assert advice.recommended is None or 0 <= advice.recommended < len(issue.fix_options), issue.id
        assert advice.why, issue.id


def test_blocking_issues_and_policy_questions_need_a_decision_the_rest_is_sign_off(tmp_path):
    for issue in all_issues(tmp_path):
        expected = issue.severity.value == "blocking" or issue.check in {"status_not_in_policy", "currency_mismatch"}
        assert guide(issue).needs_decision is expected, issue.id


def test_duplicates_recommend_review_and_coverage_gaps_recommend_acknowledging(tmp_path):
    (tmp_path / "d").mkdir()
    issues = issues_by_id(inspect(tmp_path / "d", write_sources(tmp_path / "d", oms=DIRTY_OMS)))

    duplicate = recommended(issues["duplicate_key:oms:order_id"])
    assert (duplicate.id, duplicate.params["strategy"]) == (DUPLICATE_STRATEGY, "review")

    gap = issues["coverage:oms:no_payment"]
    assert recommended(gap).id == ACKNOWLEDGE
    assert "MISSING_PAYMENT" in guide(gap).next_step


def test_status_recommendations_follow_what_the_status_means(tmp_path):
    (tmp_path / "s").mkdir()
    issues = issues_by_id(inspect(tmp_path / "s", with_status(write_sources(tmp_path / "s", oms=DIRTY_OMS))))

    canceled = recommended(issues["status_not_in_policy:canceled"])
    assert (canceled.id, canceled.params["evidence"]) == (SET_STATUS_POLICY, [])
    invoiced = recommended(issues["status_not_in_policy:invoiced"])
    assert (invoiced.id, invoiced.params["evidence"]) == (SET_STATUS_POLICY, ["payment"])
    unknown = issues["status_not_in_policy:on_hold_by_robot"]
    assert recommended(unknown).id == ACKNOWLEDGE
    assert "ORDER_STATUS_REQUIRES_POLICY" in guide(unknown).next_step


def test_issue_covering_a_large_share_of_a_feed_carries_a_caution(tmp_path):
    (tmp_path / "c").mkdir()
    unpaid = OMS + "".join(f"ORD2024-00000{n},T{n},PREPAID,10.00,2024-01-06T10:00:00,delivered\n" for n in range(10, 20))
    issues = issues_by_id(inspect(tmp_path / "c", write_sources(tmp_path / "c", oms=unpaid)))

    assert guide(issues["coverage:oms:no_payment"]).caution  # 11 of 13 orders unpaid
    (tmp_path / "small").mkdir()
    small = issues_by_id(inspect(tmp_path / "small", write_sources(tmp_path / "small", oms=OMS + "".join(
        f"ORD2024-00000{n},T{n},PREPAID,10.00,2024-01-06T10:00:00,delivered\n" for n in range(10, 20)),
        bank=BANK + "".join(f"P{n},PAYMENT ORD2024-00000{n},10.00,0,10.00,2024-01-07\n" for n in range(10, 20)))))
    assert guide(small["coverage:oms:no_payment"]).caution is None  # 1 of 13


def test_health_report_exports_carry_the_guidance(tmp_path):
    (tmp_path / "e").mkdir()
    report = inspect(tmp_path / "e", write_sources(tmp_path / "e", oms=DIRTY_OMS))

    exported = {issue["id"]: issue for issue in json.loads(report.to_json())["issues"]}
    advice = exported["duplicate_key:oms:order_id"]["guidance"]
    assert advice["needs_decision"] is True
    assert advice["recommended_option"] == "Route all duplicates to review"
    assert guide(report.issues[0]).meaning in report.to_html()
