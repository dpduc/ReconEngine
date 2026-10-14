"""Reverse pass: bank and logistics records no unit linked become orphan exceptions.

Runs after the whole funnel, so it picks up whatever every linking pass left unlinked.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from recon.funnel.base import RULE_VERSION, decide, lineage_prefixed
from recon.funnel.linking import CANDIDATE_ROWS
from recon.funnel.reasons import EXCEPTION_CONFIDENCE
from recon.schema import EXCEPTION, SOURCE_ROW

REVERSE_PASS = "reverse_pass"
ORPHAN_CODES = {"bank": "ORPHAN_PAYMENT", "logistics": "ORPHAN_SHIPMENT"}


def _column(frame: pd.DataFrame, column: str) -> pd.Series:
    return frame[column] if column in frame else pd.Series(dtype=object)


def reverse_pass(results: pd.DataFrame, feeds: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, dict[str, Any]]:
    """One EXCEPTION row per bank/logistics record whose source row no unit carries.

    Bank records a unit lists as pass 3 candidates are already in front of a reviewer with that
    unit, so they are examined but passed on. Returns (orphans, funnel counts row).
    """
    orphans, examined, candidates = [], 0, 0
    for feed, code in ORPHAN_CODES.items():
        linked = set(_column(results, f"{feed}_{SOURCE_ROW}").dropna())
        listed = set()
        if feed == "bank":
            listed = {int(r) for rows in _column(results, CANDIDATE_ROWS).dropna() for r in rows.split("|")}
        frame = feeds[feed]
        unlinked = frame[~frame[SOURCE_ROW].isin(linked)]
        is_candidate = unlinked[SOURCE_ROW].isin(listed)
        examined, candidates = examined + len(unlinked), candidates + int(is_candidate.sum())
        orphaned = lineage_prefixed(unlinked[~is_candidate], feed)
        if len(orphaned):
            orphaned = orphaned.assign(orphan_feed=feed, input_row_hashes=orphaned[f"{feed}_row_hash"])
            orphans.append(decide(orphaned, EXCEPTION, EXCEPTION_CONFIDENCE, code))
    counts = {"step": REVERSE_PASS, "units_in": examined, "decided": examined - candidates,
              "passed_on": candidates}
    if not orphans:
        return pd.DataFrame(columns=["orphan_feed", "reason_codes", "decision", "confidence",
                                     "explanation", "deciding_step", "rule_version", "input_row_hashes"]), counts
    out = pd.concat(orphans, ignore_index=True)
    out["deciding_step"] = REVERSE_PASS
    out["rule_version"] = RULE_VERSION
    return out, counts


def orphan_counts(orphans: pd.DataFrame) -> dict[str, int]:
    return {code: int((orphans["reason_codes"] == code).sum()) for code in ORPHAN_CODES.values()}
