"""Draft a run config from raw exports: format detection, mapping suggestion, mapping profiles.

Suggestions score each (canonical column, source column) pair on header similarity
and on the shape of the values. Low-confidence or ambiguous suggestions are flagged
and left out of the draft mapping, so ``inspect`` reports them as missing columns
with a ``remap`` fix instead of silently applying a guess.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd

from recon.config import FeedSpec, RunConfig
from recon.ingest import READERS, detect_delimiter, detect_encoding
from recon.schema import REQUIRED_COLUMNS

FORMATS = {".csv": "csv", ".txt": "csv", ".tsv": "csv", ".xlsx": "xlsx", ".xlsm": "xlsx",
           ".parquet": "parquet", ".pq": "parquet"}

# Header names other systems use for each canonical column.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "order_id": ("order id", "order no", "order number", "order ref", "order code"),
    "tracking_no": ("tracking", "tracking number", "awb", "waybill", "shipment id"),
    "payment_method": ("payment method", "pay method", "payment type", "tender", "method"),
    "order_amount": ("amount", "total", "order total", "order value", "grand total"),
    "ordered_at": ("order date", "order time", "placed", "placed at", "created at", "purchase date"),
    "txn_ref": ("ref", "reference", "bank ref", "transaction id", "txn id", "transaction ref"),
    "description": ("memo", "narrative", "details", "remarks", "particulars"),
    "credit_amount": ("credit", "gross", "deposit", "amount in"),
    "fee_amount": ("fee", "fees", "charges", "charge", "mdr", "commission"),
    "net_amount": ("net", "net settled", "settled amount"),
    "value_date": ("value date", "settlement date", "posting date"),
    "tracking_code": ("tracking", "tracking number", "awb", "waybill", "code"),
    "client_order_code": ("merchant order", "client ref", "order ref", "order code", "order id"),
    "cod_collected": ("cod", "cod amount", "cash collected", "collected"),
    "ship_fee": ("shipping fee", "freight", "delivery fee", "ship cost", "shipping"),
    "cod_fee": ("cod fee", "cod charge", "collection fee"),
}

# Value shapes each canonical column is expected to have.
KINDS: dict[str, frozenset[str]] = {
    "order_id": frozenset({"hex32", "id"}), "client_order_code": frozenset({"hex32", "id"}),
    "tracking_no": frozenset({"prefixed_id", "id"}), "tracking_code": frozenset({"prefixed_id", "id"}),
    "txn_ref": frozenset({"prefixed_id", "id"}),
    "payment_method": frozenset({"category"}), "description": frozenset({"text"}),
    "ordered_at": frozenset({"date"}), "value_date": frozenset({"date"}),
    **{c: frozenset({"money"}) for c in ("order_amount", "credit_amount", "fee_amount", "net_amount",
                                         "cod_collected", "ship_fee", "cod_fee")},
}

NAME_WEIGHT, KIND_WEIGHT = 0.6, 0.4
MIN_CONFIDENCE = 0.6  # below this a suggestion is flagged
AMBIGUITY_MARGIN = 0.1  # a rival column this close to the best one makes it ambiguous
SAMPLE_ROWS = 500

_HEX32 = re.compile(r"[0-9a-f]{32}")
_PREFIXED = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)*-[0-9A-Za-z]+")  # OLIST-<id>, OLIST-PAY-<id>
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?"
                   r"|\d{1,2}[/.]\d{1,2}[/.]\d{2,4}(?: \d{1,2}:\d{2}(?::\d{2})?)?")


@dataclass(frozen=True)
class Suggestion:
    feed: str
    canonical: str
    source: str | None  # suggested source column; None when nothing fits
    confidence: float
    flagged: bool  # True = needs the analyst; not applied to the draft mapping
    reason: str
    alternatives: tuple[str, ...] = ()  # other plausible source columns, best first


@dataclass(frozen=True)
class DraftConfig:
    config: RunConfig  # mapping holds only unflagged suggestions
    suggestions: dict[str, dict[str, Suggestion]]  # feed -> canonical -> suggestion
    columns: dict[str, tuple[str, ...]] = field(default_factory=dict)  # feed -> source columns, file order

    @property
    def flagged(self) -> list[Suggestion]:
        return [s for cols in self.suggestions.values() for s in cols.values() if s.flagged]


@dataclass(frozen=True)
class MappingProfile:
    """A confirmed `{canonical: source column}` mapping for one feed of one source system."""

    source_system: str
    feed: str
    mapping: dict[str, str] = field(default_factory=dict)

    def matches(self, columns: list[str]) -> bool:
        return set(self.mapping.values()) <= set(columns)


def save_profile(profile: MappingProfile, path: str | Path) -> None:
    Path(path).write_text(json.dumps(asdict(profile), indent=2, sort_keys=True), encoding="utf-8")


def load_profile(path: str | Path) -> MappingProfile:
    return MappingProfile(**json.loads(Path(path).read_text(encoding="utf-8")))


def _normalize(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", name.lower()).split())


def _name_score(canonical: str, column: str) -> float:
    column = _normalize(column)
    tokens = set(column.split())
    best = 0.0
    for alias in (canonical.replace("_", " "),) + SYNONYMS.get(canonical, ()):
        if alias == column:
            return 1.0
        alias_tokens = set(alias.split())
        contained = 0.8 if tokens and (alias_tokens <= tokens or tokens <= alias_tokens) else 0.0
        best = max(best, contained, SequenceMatcher(None, alias, column).ratio())
    return best


def _kind(values: pd.Series) -> str:
    values = values[values.str.strip() != ""]
    if values.empty:
        return "empty"
    if values.str.fullmatch(_HEX32).all():
        return "hex32"
    if values.str.fullmatch(_PREFIXED).all() and values.str.contains(r"\d").all():
        return "prefixed_id"
    if values.str.fullmatch(_DATE).all():
        return "date"
    if pd.to_numeric(values, errors="coerce").notna().all():
        return "money"
    if (~values.str.contains(r"\s")).all() and values.str.contains(r"\d").all() and values.is_unique:
        return "id"
    if (~values.str.contains(r"\d")).all() and values.str.len().max() <= 24:
        return "category"
    return "text"


def suggest_mapping(feed: str, frame: pd.DataFrame) -> dict[str, Suggestion]:
    """Suggest a source column for each required canonical column of `feed`.

    `frame` is the raw, all-string export. Each source column is used at most once.
    """
    sample = frame.head(SAMPLE_ROWS)
    kinds = {column: _kind(sample[column]) for column in sample.columns}
    canonicals = REQUIRED_COLUMNS[feed]
    score = {
        (c, s): NAME_WEIGHT * _name_score(c, s) + KIND_WEIGHT * (kinds[s] in KINDS[c])
        for c in canonicals for s in sample.columns
    }
    assigned: dict[str, str] = {}
    for (c, s), _ in sorted(score.items(), key=lambda kv: -kv[1]):
        if c not in assigned and s not in assigned.values():
            assigned[c] = s
    owner = {s: c for c, s in assigned.items()}

    suggestions = {}
    for c in canonicals:
        source = assigned.get(c)
        if source is None:
            suggestions[c] = Suggestion(feed, c, None, 0.0, True, "no source column left")
            continue
        confidence = round(score[(c, source)], 3)
        ranked = sorted((s for s in sample.columns if s != source), key=lambda s: -score[(c, s)])
        alternatives = tuple(s for s in ranked if score[(c, s)] >= MIN_CONFIDENCE)
        # A rival is a close runner-up that isn't clearly better used elsewhere.
        rivals = [s for s in ranked
                  if score[(c, s)] >= confidence - AMBIGUITY_MARGIN
                  and not (s in owner and score[(owner[s], s)] > score[(c, s)] + AMBIGUITY_MARGIN)]
        if confidence < MIN_CONFIDENCE:
            flagged, reason = True, f"low confidence ({kinds[source]} values)"
        elif rivals:
            flagged, reason = True, f"ambiguous with {', '.join(rivals)}"
        else:
            flagged, reason = False, f"header match, {kinds[source]} values"
        suggestions[c] = Suggestion(feed, c, source, confidence, flagged, reason, alternatives)
    return suggestions


def _feed_spec(folder: Path, path: str) -> FeedSpec:
    fmt = FORMATS.get(Path(path).suffix.lower())
    if fmt is None:
        raise ValueError(f"{path}: cannot infer format from extension")
    if fmt != "csv":
        return FeedSpec(path, format=fmt)
    encoding = detect_encoding(folder / path)
    return FeedSpec(path, format="csv", encoding=encoding, delimiter=detect_delimiter(folder / path, encoding))


def draft_config(sources: str | Path, files: dict[str, str],
                 profiles: list[MappingProfile] | tuple[MappingProfile, ...] = (),
                 base: RunConfig | None = None) -> DraftConfig:
    """Draft a run config for `files` ({feed: path relative to `sources`}).

    A profile for the feed whose columns are all present is reused verbatim;
    otherwise the mapping is suggested. Policy and ground truth come from `base`.
    """
    folder = Path(sources)
    feeds, mapping, suggestions, columns = {}, {}, {}, {}
    for feed, path in files.items():
        spec = feeds[feed] = _feed_spec(folder, path)
        raw = READERS[spec.format](folder / path, spec)
        columns[feed] = tuple(raw.columns)
        profile = next((p for p in profiles if p.feed == feed and p.matches(list(raw.columns))), None)
        if profile is not None:
            suggestions[feed] = {
                c: Suggestion(feed, c, s, 1.0, False, f"profile '{profile.source_system}'")
                for c, s in profile.mapping.items()
            }
        else:
            suggestions[feed] = suggest_mapping(feed, raw)
        mapping[feed] = {c: s.source for c, s in suggestions[feed].items() if not s.flagged}
    base = base or RunConfig(feeds={})
    config = RunConfig(feeds=feeds, mapping=mapping, policy=base.policy,
                       resolutions=base.resolutions, ground_truth=base.ground_truth)
    return DraftConfig(config, suggestions, columns)
