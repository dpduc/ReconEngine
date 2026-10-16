"""Scoring unit results against ground truth, and summary metrics."""

from __future__ import annotations

from typing import Any

import pandas as pd

from recon.schema import AUTO_ACCEPT, EXCEPTION, REVIEW


def attach_truth(results: pd.DataFrame, truth: pd.DataFrame) -> pd.DataFrame:
    truth = truth.drop_duplicates("order_id").rename(
        columns={"expected_outcome": "truth_outcome", "expected_exception": "truth_exception"}
    )
    out = results.merge(truth[["order_id", "scenario", "sub_kind", "truth_outcome", "truth_exception"]], on="order_id", how="left")
    out["truth_decision"] = out["truth_outcome"].map(
        lambda value: AUTO_ACCEPT if value in {"PASS_1", "PASS_2"} else EXCEPTION
    )
    out["is_correct"] = out["decision"].eq(out["truth_decision"])
    return out


CLEAN_CODES = {"THREE_WAY_TIE_OUT", "SETTLEMENT_TIE_OUT", "REMITTANCE_TIE_OUT"}  # reason codes that describe a match, not a problem


def _codes(value: Any) -> set[str]:
    return {c for c in str(value).split("|") if c and c not in CLEAN_CODES} if isinstance(value, str) else set()


def _ratio(part: float, whole: float) -> float | None:
    return part / whole if whole else None


def per_reason_code(results: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Precision/recall of each predicted reason code against `truth_exception`, on rows with truth.

    Each is given by unit count and weighted by the order amount: `amount_caught` is the money
    on units where the code was both predicted and true, `amount_missed` the money where it was
    true but not predicted.
    """
    if "truth_outcome" not in results:
        return {}
    scored = results[results["truth_outcome"].notna()]
    predicted = [_codes(v) for v in scored["reason_codes"]]
    actual = [_codes(v) for v in scored["truth_exception"]]
    amounts = pd.to_numeric(scored["order_amount"], errors="coerce").fillna(0).abs().tolist() \
        if "order_amount" in scored else [0.0] * len(scored)
    out: dict[str, dict[str, Any]] = {}
    for code in sorted(set().union(*predicted, *actual)):
        flags = [(code in p, code in a, amount) for p, a, amount in zip(predicted, actual, amounts)]
        n_pred, n_true = sum(p for p, _, _ in flags), sum(a for _, a, _ in flags)
        hits = sum(p and a for p, a, _ in flags)
        money_pred = sum(amount for p, _, amount in flags if p)
        money_true = sum(amount for _, a, amount in flags if a)
        caught = sum(amount for p, a, amount in flags if p and a)
        out[code] = {"predicted": n_pred, "actual": n_true, "true_positives": hits,
                     "precision": _ratio(hits, n_pred), "recall": _ratio(hits, n_true),
                     "amount_predicted": money_pred, "amount_actual": money_true,
                     "amount_caught": caught, "amount_missed": money_true - caught,
                     "amount_precision": _ratio(caught, money_pred), "amount_recall": _ratio(caught, money_true)}
    return out


# --- links -----------------------------------------------------------------------------------------

LINK_TRUTH_COLUMNS = ("feed", "record_key", "order_id")
LINKED_FEEDS = {"bank": ("txn_ref", "bank_link_pass"), "logistics": ("tracking_code", "logistics_link_pass")}
CANDIDATES = "candidates"  # pass-3 proposals: REVIEW, never a link


def read_link_truth(truth: pd.DataFrame) -> pd.DataFrame:
    """True links, one row per (feed, record key, order); rows naming no order are dropped."""
    missing = [column for column in LINK_TRUTH_COLUMNS if column not in truth]
    if missing:
        raise ValueError(f"link truth needs the columns {', '.join(LINK_TRUTH_COLUMNS)}; missing {', '.join(missing)}")
    truth = truth.assign(link_kind=truth["link_kind"] if "link_kind" in truth else "")
    truth = truth[truth["order_id"].notna() & truth["order_id"].astype(str).ne("")]
    columns = [*LINK_TRUTH_COLUMNS, "link_kind"]
    return truth[columns].astype(str).drop_duplicates(list(LINK_TRUTH_COLUMNS)).reset_index(drop=True)


def _predicted_links(results: pd.DataFrame) -> pd.DataFrame:
    frames = []
    if "settlement_order_no" in results:  # a unit's line in its channel's settlement report
        linked = results[results["settlement_order_no"].notna()]
        frames.append(pd.DataFrame({"feed": linked["settlement_feed"], "record_key": linked["settlement_order_no"]
                                    .astype(str), "order_id": linked["order_id"].astype(str), "link_pass": "exact"}))
    for feed, (key, link_pass) in LINKED_FEEDS.items():
        if key in results and link_pass in results:
            linked = results[results[key].notna()]
            frames.append(pd.DataFrame({"feed": feed, "record_key": linked[key].astype(str),
                                        "order_id": linked["order_id"].astype(str), "link_pass": linked[link_pass]}))
    if "remittance_txn_ref" in results:  # the remittance a unit's shipment is on, when not its own credit
        linked = results[results["remittance_txn_ref"].notna() & results["remittance_txn_ref"].ne(results["txn_ref"])]
        frames.append(pd.DataFrame({"feed": "bank", "record_key": linked["remittance_txn_ref"].astype(str),
                                    "order_id": linked["order_id"].astype(str), "link_pass": "remittance"}))
    if not frames:
        return pd.DataFrame(columns=["feed", "record_key", "order_id", "link_pass"])
    links = pd.concat(frames, ignore_index=True)  # a split payment links each of its credits
    return links.assign(record_key=links["record_key"].str.split("|")).explode("record_key", ignore_index=True)


def _candidate_metrics(results: pd.DataFrame, true_bank: set[tuple[str, str]]) -> dict[str, Any]:
    """Pass-3 proposals: how often the best candidate, or any candidate, is a true bank link."""
    if "link_candidates" not in results:
        return {"units": 0, "best_correct": None, "any_correct": None}
    proposed = results[results["link_candidates"].notna()]
    best = any_ = 0
    for order_id, listed in zip(proposed["order_id"].astype(str), proposed["link_candidates"]):
        refs = [item.rsplit(":", 1)[0] for item in str(listed).split("|") if item]
        best += bool(refs) and (refs[0], order_id) in true_bank
        any_ += any((ref, order_id) in true_bank for ref in refs)
    return {"units": int(len(proposed)), "best_correct": _ratio(best, len(proposed)),
            "any_correct": _ratio(any_, len(proposed))}


def link_metrics(results: pd.DataFrame, truth: pd.DataFrame,
                 settlement_feeds: tuple[str, ...] = ()) -> dict[str, Any]:
    """Precision and recall of the links a run made, against true links.

    Only true links of orders in this run and of feeds the engine links (bank, logistics and the
    marketplaces' `settlement_feeds`) can be found; the others are counted under `not_scored` (e.g.
    a settlement report the run did not read, or an order from an earlier period). Precision is per linking pass, recall per feed and per
    true link kind (e.g. `order_ref`, `payout_batch`).
    """
    truth = read_link_truth(truth)
    in_run = truth["order_id"].isin(results["order_id"].astype(str).unique().tolist())
    feeds = [*LINKED_FEEDS, *dict.fromkeys(settlement_feeds)]
    linkable = truth["feed"].isin(feeds)
    scored_truth = truth[linkable & in_run]
    keys = list(LINK_TRUTH_COLUMNS)
    predicted = _predicted_links(results)
    predicted["correct"] = predicted[keys].merge(scored_truth[keys].assign(_true=True), on=keys,
                                                 how="left")["_true"].fillna(False).astype(bool).to_numpy()
    found = predicted.loc[predicted["correct"], keys].drop_duplicates().assign(_found=True)
    scored_truth = scored_truth.assign(found=scored_truth[keys].merge(found, on=keys, how="left")["_found"]
                                       .fillna(False).astype(bool).to_numpy())

    def precision(frame: pd.DataFrame) -> dict[str, Any]:
        return {"links": int(len(frame)), "correct": int(frame["correct"].sum()),
                "precision": _ratio(int(frame["correct"].sum()), len(frame))}

    def recall(frame: pd.DataFrame) -> dict[str, Any]:
        return {"true_links": int(len(frame)), "found": int(frame["found"].sum()),
                "recall": _ratio(int(frame["found"].sum()), len(frame))}

    not_scored = truth[~(linkable & in_run)]
    bank_truth = scored_truth[scored_truth["feed"] == "bank"]
    true_bank = set(zip(bank_truth["record_key"], bank_truth["order_id"]))
    return {
        "overall": {**precision(predicted), **recall(scored_truth)},
        "per_pass": {f"{feed}:{link_pass}": precision(group)
                     for (feed, link_pass), group in predicted.groupby(["feed", "link_pass"], sort=True)},
        "per_feed": {feed: {**precision(predicted[predicted["feed"] == feed]),
                            **recall(scored_truth[scored_truth["feed"] == feed])} for feed in feeds},
        "per_link_kind": {f"{feed}:{kind}": recall(group)
                          for (feed, kind), group in scored_truth.groupby(["feed", "link_kind"], sort=True)},
        CANDIDATES: _candidate_metrics(results, true_bank),
        "not_scored": {"feed_not_linked": int((~linkable).sum()), "order_not_in_run": int((linkable & ~in_run).sum())},
    }


def metrics(results: pd.DataFrame) -> dict[str, Any]:
    accepted = results[results["decision"] == AUTO_ACCEPT]
    reviewed = results[results["decision"] == REVIEW]
    truth_available = results["truth_decision"].notna() if "truth_decision" in results else pd.Series(False, index=results.index)
    scored = results[truth_available]
    return {
        "rows": int(len(results)),
        "auto_accepts": int(len(accepted)),
        "review_queue": int(len(reviewed)),
        "review_rate": float(len(reviewed) / len(results)) if len(results) else 0.0,
        "auto_accept_precision": float(accepted["is_correct"].mean()) if len(accepted) and "is_correct" in accepted else None,
        "accuracy": float(scored["is_correct"].mean()) if len(scored) else None,
        "scenario_counts": {k: int(v) for k, v in results["reason_codes"].value_counts().head(12).items()},
        "per_reason_code": per_reason_code(results),
    }
