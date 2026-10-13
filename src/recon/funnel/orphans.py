"""Reverse pass: bank and logistics records no unit linked become orphan exceptions.

Runs after the whole funnel, so it picks up whatever every linking pass left unlinked.
"""

from __future__ import annotations

import pandas as pd

from recon.funnel.base import RULE_VERSION, decide, lineage_prefixed
from recon.funnel.rules import EXCEPTION_CONFIDENCE
from recon.schema import SOURCE_ROW

REVERSE_PASS = "reverse_pass"
ORPHAN_CODES = {"bank": "ORPHAN_PAYMENT", "logistics": "ORPHAN_SHIPMENT"}


def reverse_pass(results: pd.DataFrame, feeds: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """One EXCEPTION row per bank/logistics record whose source row no unit carries."""
    orphans = []
    for feed, code in ORPHAN_CODES.items():
        linked_column = f"{feed}_{SOURCE_ROW}"
        linked = results[linked_column].dropna() if linked_column in results else pd.Series(dtype=float)
        frame = feeds[feed]
        unlinked = lineage_prefixed(frame[~frame[SOURCE_ROW].isin(linked)], feed)
        if len(unlinked):
            unlinked = unlinked.assign(orphan_feed=feed, input_row_hashes=unlinked[f"{feed}_row_hash"])
            orphans.append(decide(unlinked, "EXCEPTION", EXCEPTION_CONFIDENCE, code))
    if not orphans:
        return pd.DataFrame(columns=["orphan_feed", "reason_codes", "decision", "confidence",
                                     "explanation", "deciding_step", "rule_version", "input_row_hashes"])
    out = pd.concat(orphans, ignore_index=True)
    out["deciding_step"] = REVERSE_PASS
    out["rule_version"] = RULE_VERSION
    return out


def orphan_counts(orphans: pd.DataFrame) -> dict[str, int]:
    return {code: int((orphans["reason_codes"] == code).sum()) for code in ORPHAN_CODES.values()}
