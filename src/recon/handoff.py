"""Expert handoff: resolution actions on the exception queue, an append-only audit log, and metrics over it.

The engine's work ends at the exception queue; this module records what a person does next. Each
action is one line appended to a JSON Lines file, with who, when, the action, the note and the
engine's decision at the time. Nothing is rewritten: a changed mind is a new line (another action,
or ``reopen``). Everything else (the unresolved queue, handoff metrics, close status) is computed from
the log and a run, so it survives a reload.

Queue items are identified by ``(run_key, record_type, record_ref)``. The run key is the hash of
the run's input files and rule version: rerunning the same period's data keeps the same queue,
while new data starts a new one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from recon.results import ReconRun

# Resolution actions. `override` matches the record to one the expert chose (engine disagreement);
# `reopen` puts a resolved item back in the unresolved queue.
CONFIRM, OVERRIDE, WRITE_OFF, MARK_REFUND, REOPEN = "confirm", "override", "write_off", "mark_refund", "reopen"
RESOLVING_ACTIONS = (CONFIRM, OVERRIDE, WRITE_OFF, MARK_REFUND)
ACTIONS = RESOLVING_ACTIONS + (REOPEN,)
ACTION_LABELS = {CONFIRM: "Confirm", OVERRIDE: "Override (match to a record)", WRITE_OFF: "Write off",
                 MARK_REFUND: "Mark as refund", REOPEN: "Reopen"}
NOTE_REQUIRED = (OVERRIDE, WRITE_OFF, MARK_REFUND, REOPEN)

QUEUED = "queued"  # log event: a run's queue was first presented; starts the resolution clock

# What an expert is advised to do per reason code (a unit's first code), and why.
RECOMMENDED_ACTIONS: dict[str, tuple[str, str]] = {
    "MISSING_PAYMENT": (OVERRIDE, "find the credit that pays it and match it; write off if none will arrive"),
    "AMOUNT_MISMATCH": (CONFIRM, "confirm the difference and claim it from the payer"),
    "CURRENCY_MISMATCH": (CONFIRM, "confirm, then set an FX rate source or correct the currency"),
    "FX_DIFFERENCE": (WRITE_OFF, "a small conversion difference; write it off against FX"),
    "MISSING_SHIPMENT": (CONFIRM, "confirm and ask the 3PL for proof of shipment"),
    "FEE_MISMATCH": (CONFIRM, "confirm and claim the excess fee from the PSP"),
    "COD_FEE_MISMATCH": (CONFIRM, "confirm and claim the excess COD fee from the 3PL"),
    "MISSING_SOURCE_EVIDENCE": (CONFIRM, "confirm and get the order amount from the OMS"),
    "ORDER_STATUS_REQUIRES_POLICY": (CONFIRM, "confirm and set an evidence policy for the status"),
    "TIMING_MISMATCH": (CONFIRM, "confirm the event dates with the 3PL"),
    "LINK_CANDIDATES": (OVERRIDE, "pick the right credit among the candidates"),
    "POSSIBLE_INSTALLMENT_INTEREST": (CONFIRM, "confirm the delta is installment interest with the PSP"),
    "DUPLICATE_PAYMENT": (MARK_REFUND, "one credit alone pays it; refund the other or apply it elsewhere"),
    "SETTLEMENT_BATCH_MISMATCH": (CONFIRM, "confirm the payout's difference with the platform or carrier"),
    "PAYOUT_CREDIT_AMBIGUOUS": (OVERRIDE, "pick the bank credit that is the payout"),
    "SETTLEMENT_REPORT_MISSING": (OVERRIDE, "load the channel's settlement report, or pick the payout that pays it"),
    "ORPHAN_PAYMENT": (OVERRIDE, "match it to the order it pays; mark as refund if it pays none"),
    "ORPHAN_SHIPMENT": (OVERRIDE, "match it to the order it ships"),
}


def primary_code(reason_codes: Any) -> str:
    """The first reason code of a unit (the check it failed first), or "" when it has none."""
    return str(reason_codes).split("|")[0] if isinstance(reason_codes, str) else ""


def recommended_action(reason_codes: Any) -> tuple[str, str] | None:
    """(action, why) for a unit's first reason code; duplicate-key routing recommends a confirm."""
    code = primary_code(reason_codes)
    if code.startswith("DUPLICATE_") and code.endswith("_KEY"):
        return CONFIRM, "check which of the duplicate rows is the real one, then confirm"
    return RECOMMENDED_ACTIONS.get(code)


def run_key(outcome: ReconRun) -> str:
    """Identity of a run's queue: its input files' hashes and rule version (not when it ran)."""
    files = outcome.manifest.get("input_files", {})
    payload = json.dumps({"rule_version": outcome.manifest.get("rule_version"),
                          "inputs": {name: f.get("sha256") for name, f in sorted(files.items())}}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class AuditEntry:
    at: str  # ISO 8601, UTC
    event: str  # an action in ACTIONS, or QUEUED
    run_key: str
    actor: str = ""
    record_type: str = ""  # oms, bank or logistics
    record_ref: str = ""
    note: str = ""
    matched_ref: str = ""  # override: the record the expert matched it to
    # The engine's decision at the time of the action.
    engine_decision: str = ""
    engine_reason_codes: str = ""
    engine_step: str = ""
    amount_at_risk: float | None = None
    queue_items: int | None = None  # QUEUED: how many items the queue held

    def to_json(self) -> str:
        return json.dumps({k: v for k, v in asdict(self).items() if v not in ("", None)}, sort_keys=True)


@dataclass
class AuditLog:
    """Append-only JSON Lines log at `path`; created on the first write."""

    path: Path
    _entries: list[AuditEntry] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    def entries(self) -> list[AuditEntry]:
        if self._entries is None:
            self._entries = []
            if self.path.exists():
                with self.path.open(encoding="utf-8") as lines:
                    self._entries = [AuditEntry(**json.loads(line)) for line in lines if line.strip()]
        return list(self._entries)

    def frame(self) -> pd.DataFrame:
        columns = list(AuditEntry.__dataclass_fields__)
        return pd.DataFrame([asdict(e) for e in self.entries()], columns=columns)

    def _append(self, entry: AuditEntry) -> AuditEntry:
        entries = self.entries()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as out:
            out.write(entry.to_json() + "\n")
        self._entries = entries + [entry]
        return entry

    def register_queue(self, outcome: ReconRun, at: str | None = None) -> None:
        """Log when a run's queue is first presented; later calls for the same run key do nothing."""
        key = run_key(outcome)
        if not any(e.event == QUEUED and e.run_key == key for e in self.entries()):
            self._append(AuditEntry(at=at or _now(), event=QUEUED, run_key=key,
                                    queue_items=int(len(outcome.exception_queue))))

    def record(self, outcome: ReconRun, record_type: str, record_ref: str, action: str, actor: str,
               note: str = "", matched_ref: str = "", at: str | None = None) -> AuditEntry:
        """Append one resolution action on a queue item of `outcome`, with the engine's decision on it."""
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}; expected one of {ACTIONS}")
        if not actor.strip():
            raise ValueError("an action needs the name of who takes it")
        if action in NOTE_REQUIRED and not note.strip():
            raise ValueError(f"{ACTION_LABELS[action]} needs a note")
        if action == OVERRIDE and not str(matched_ref).strip():
            raise ValueError("an override needs the record it is matched to")
        queue = outcome.exception_queue
        item = queue[(queue["record_type"] == record_type) & (queue["record_ref"].astype(str) == str(record_ref))]
        if item.empty:
            raise ValueError(f"{record_type} {record_ref} is not in the run's exception queue")
        resolved = _item_state(self.entries(), run_key(outcome)).get((record_type, str(record_ref)))
        if action == REOPEN and resolved is None:
            raise ValueError(f"{record_type} {record_ref} is not resolved; there is nothing to reopen")
        if action != REOPEN and resolved is not None:
            raise ValueError(f"{record_type} {record_ref} is already resolved; reopen it first")
        self.register_queue(outcome, at)
        row = item.iloc[0]
        return self._append(AuditEntry(
            at=at or _now(), event=action, run_key=run_key(outcome), actor=actor.strip(),
            record_type=record_type, record_ref=str(record_ref), note=note.strip(),
            matched_ref=str(matched_ref).strip() if action == OVERRIDE else "",
            engine_decision=str(row["decision"]), engine_reason_codes=str(row["reason_codes"]),
            engine_step=str(row.get("deciding_step", "")),
            amount_at_risk=round(float(row["amount_at_risk"]), 2)))


def _item_state(entries: list[AuditEntry], key: str) -> dict[tuple[str, str], AuditEntry]:
    """{(record_type, record_ref): resolving entry} for items of run `key` resolved now (not reopened)."""
    state: dict[tuple[str, str], AuditEntry] = {}
    for entry in entries:
        if entry.run_key != key or entry.event not in ACTIONS:
            continue
        item = (entry.record_type, entry.record_ref)
        if entry.event == REOPEN:
            state.pop(item, None)
        else:
            state[item] = entry
    return state


def with_recommendations(queue: pd.DataFrame) -> pd.DataFrame:
    """`queue` plus `recommended_action` and `recommended_why` per item (empty when none applies)."""
    advice = queue["reason_codes"].map(recommended_action) if len(queue) else pd.Series(dtype=object)
    return queue.assign(recommended_action=advice.map(lambda a: a[0] if a else ""),
                        recommended_why=advice.map(lambda a: a[1] if a else ""))


def unresolved_queue(outcome: ReconRun, log: AuditLog) -> pd.DataFrame:
    """The exception queue without the items resolved in `log`, in queue order, with recommendations."""
    queue = with_recommendations(outcome.exception_queue)
    resolved = _item_state(log.entries(), run_key(outcome))
    keep = [(t, str(r)) not in resolved for t, r in zip(queue["record_type"], queue["record_ref"], strict=True)]
    return queue[keep].reset_index(drop=True)


def resolved_items(outcome: ReconRun, log: AuditLog) -> pd.DataFrame:
    """One row per item of `outcome` resolved now: its resolving entry."""
    resolved = _item_state(log.entries(), run_key(outcome))
    columns = list(AuditEntry.__dataclass_fields__)
    return pd.DataFrame([asdict(e) for e in resolved.values()], columns=columns)


def evidence_complete(queue: pd.DataFrame) -> pd.Series:
    """Per queue item: source rows traced, an explanation, and a recommended action are all present."""
    if queue.empty:
        return pd.Series(dtype=bool)
    rows = queue["input_row_hashes"].fillna("").astype(str).str.len().gt(0) if "input_row_hashes" in queue \
        else pd.Series(False, index=queue.index)
    explained = queue["explanation"].fillna("").astype(str).str.strip().str.len().gt(0) if "explanation" in queue \
        else pd.Series(False, index=queue.index)
    advised = queue["reason_codes"].map(lambda codes: recommended_action(codes) is not None)
    return rows & explained & advised


def _hours(later: pd.Series, earlier: pd.Series) -> pd.Series:
    return (later - earlier).dt.total_seconds() / 3600


def handoff_metrics(outcome: ReconRun, log: AuditLog, now: str | None = None) -> dict[str, Any]:
    """Handoff quality for `outcome`'s queue, computed from `log`.

    - ``per_reason_code``: per first reason code, records queued / resolved / unresolved, overrides and the
      override rate (engine disagreement), median hours to resolution, and aging of unresolved records in hours.
    - ``throughput_per_day``: items resolved per UTC day.
    - ``first_touch_rate``: share of resolved items resolved by their only action (never reopened).
    - ``evidence_completeness``: share of queue items with source rows, an explanation and a
      recommended action.
    Time to resolution runs from when the queue was first presented (its ``queued`` entry).
    """
    key = run_key(outcome)
    entries = log.entries()
    queue = with_recommendations(outcome.exception_queue)
    queued = [e.at for e in entries if e.event == QUEUED and e.run_key == key]
    queued_at = pd.Timestamp(min(queued)) if queued else None
    now_at = pd.Timestamp(now or _now())
    actions = pd.DataFrame([asdict(e) for e in entries if e.run_key == key and e.event in ACTIONS],
                           columns=list(AuditEntry.__dataclass_fields__))
    actions["at"] = pd.to_datetime(actions["at"], utc=True)
    resolved = resolved_items(outcome, log)
    resolved["at"] = pd.to_datetime(resolved["at"], utc=True)
    resolved["code"] = resolved["engine_reason_codes"].map(primary_code)
    if queued_at is not None:
        resolved["hours"] = _hours(resolved["at"], pd.Series(queued_at, index=resolved.index))
    else:
        resolved["hours"] = float("nan")
    touches = actions.groupby(["record_type", "record_ref"]).size()
    resolved["first_touch"] = [touches.get((t, r), 0) == 1 for t, r in zip(resolved["record_type"],
                                                                             resolved["record_ref"], strict=True)]
    is_resolved = {(t, r) for t, r in zip(resolved["record_type"], resolved["record_ref"], strict=True)}
    queue = queue.assign(code=queue["reason_codes"].map(primary_code),
                         resolved=[(t, str(r)) in is_resolved for t, r in zip(queue["record_type"],
                                                                               queue["record_ref"], strict=True)])
    unresolved_age = (now_at - queued_at).total_seconds() / 3600 if queued_at is not None else None

    per_code: dict[str, dict[str, Any]] = {}
    for code in sorted(set(queue["code"]) | set(resolved["code"])):
        in_queue = queue[queue["code"] == code]
        done = resolved[resolved["code"] == code]
        overrides = int((done["event"] == OVERRIDE).sum())
        unresolved = int((~in_queue["resolved"]).sum())
        per_code[code or "(none)"] = {
            "queued": int(len(in_queue)), "resolved": int(len(done)), "unresolved": unresolved,
            "overrides": overrides,
            "override_rate": round(overrides / len(done), 4) if len(done) else None,
            "median_hours_to_resolution": _round(done["hours"].median()) if len(done) else None,
            # Every unresolved record of a queue has waited since the queue was presented.
            "unresolved_age_hours": _round(unresolved_age) if unresolved and unresolved_age is not None else None,
        }
    throughput = resolved.groupby(resolved["at"].dt.strftime("%Y-%m-%d")).size() if len(resolved) else {}
    complete = evidence_complete(queue)
    overrides = int((resolved["event"] == OVERRIDE).sum())
    return {
        "run_key": key,
        "queued_at": queued_at.isoformat() if queued_at is not None else None,
        "queue_items": int(len(queue)),
        "resolved": int(len(resolved)),
        "unresolved": int(len(queue) - queue["resolved"].sum()),
        "actions": {action: int((actions["event"] == action).sum()) for action in ACTIONS},
        "override_rate": round(overrides / len(resolved), 4) if len(resolved) else None,
        "median_hours_to_resolution": _round(resolved["hours"].median()) if len(resolved) else None,
        "first_touch_rate": round(float(resolved["first_touch"].mean()), 4) if len(resolved) else None,
        "evidence_completeness": round(float(complete.mean()), 4) if len(complete) else None,
        "throughput_per_day": {day: int(n) for day, n in dict(throughput).items()},
        "per_reason_code": per_code,
    }


def _round(value: Any) -> float | None:
    return None if pd.isna(value) else round(float(value), 2)


def close_status(outcome: ReconRun, log: AuditLog) -> dict[str, Any]:
    """What still blocks closing the period: unresolved queue items per reason code, by amount at risk.

    ``revisit_first`` is the close watchlist (auto-accepted with a note, e.g. installment interest):
    the first records to revisit if the period still won't close once nothing else blocks it.
    """
    unresolved = unresolved_queue(outcome, log)
    unresolved = unresolved.assign(code=unresolved["reason_codes"].map(primary_code))
    blockers = (unresolved.groupby("code").agg(records=("record_ref", "size"), amount_at_risk=("amount_at_risk", "sum"))
                .sort_values(["amount_at_risk", "records"], ascending=False).reset_index()
                .rename(columns={"code": "reason_code"}))
    blockers["amount_at_risk"] = blockers["amount_at_risk"].round(2)
    watchlist = outcome.close_watchlist
    return {
        "can_close": unresolved.empty,
        "unresolved": int(len(unresolved)),
        "amount_at_risk": round(float(unresolved["amount_at_risk"].sum()), 2),
        "blockers": blockers,
        "revisit_first": watchlist,
        "revisit_amount": round(float(watchlist["note_amount"].sum()), 2) if len(watchlist) else 0.0,
    }
