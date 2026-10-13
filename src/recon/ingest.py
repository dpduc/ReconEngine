"""Load raw feeds into the canonical schema with row-level lineage."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from recon.config import FeedSpec
from recon.schema import INGESTED_AT, NUMERIC_COLUMNS, ROW_HASH, SOURCE_FILE, SOURCE_ROW


# Format adapters: each returns every cell as a string ("" for empty) so row
# hashes are taken over the raw values, before any parsing.
Reader = Callable[[Path, FeedSpec], pd.DataFrame]


ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")  # latin-1 decodes any byte: last resort
DELIMITERS = (",", ";", "\t", "|")


def detect_encoding(path: str | Path, declared: str | None = None) -> str:
    """`declared` if the file decodes with it, else the first of ENCODINGS that does."""
    data = Path(path).read_bytes()
    for encoding in ((declared,) if declared else ()) + ENCODINGS:
        try:
            data.decode(encoding)
        except UnicodeDecodeError:
            continue
        return "utf-8-sig" if encoding == "utf-8" and data.startswith(b"\xef\xbb\xbf") else encoding
    raise AssertionError("unreachable: latin-1 decodes any byte")


def detect_delimiter(path: str | Path, encoding: str) -> str:
    """The candidate splitting the header and first data lines into the same, largest field count."""
    with open(path, encoding=encoding, newline="") as handle:
        lines = [handle.readline() for _ in range(2)]
    best, best_fields = ",", 1
    for delimiter in DELIMITERS:
        fields = [line.count(delimiter) + 1 for line in lines if line.strip()]
        if fields and len(set(fields)) == 1 and fields[0] > best_fields:
            best, best_fields = delimiter, fields[0]
    return best


def _read_csv(path: Path, spec: FeedSpec) -> pd.DataFrame:
    encoding = detect_encoding(path, spec.encoding)
    delimiter = spec.delimiter or detect_delimiter(path, encoding)
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding=encoding, sep=delimiter)


def _as_text(frame: pd.DataFrame) -> pd.DataFrame:
    """Typed cells -> strings; integral floats lose their '.0' so 100.0 reads like CSV '100'."""
    def cell(value: object) -> str:
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            return ""
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
        return str(value)

    return frame.astype(object).apply(lambda col: col.map(cell)).astype(str)


def _read_xlsx(path: Path, spec: FeedSpec) -> pd.DataFrame:
    return _as_text(pd.read_excel(path, dtype=object, keep_default_na=False))


def _read_parquet(path: Path, spec: FeedSpec) -> pd.DataFrame:
    return _as_text(pd.read_parquet(path))


READERS: dict[str, Reader] = {"csv": _read_csv, "xlsx": _read_xlsx, "parquet": _read_parquet}


@dataclass(frozen=True)
class LoadedFeed:
    name: str
    path: Path
    sha256: str
    frame: pd.DataFrame  # canonical columns + unmapped pass-through columns + lineage


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _row_hashes(raw: pd.DataFrame) -> list[str]:
    return [
        hashlib.sha256("\x1f".join(row).encode("utf-8")).hexdigest()
        for row in raw.itertuples(index=False, name=None)
    ]


def load_feed(name: str, folder: str | Path, spec: FeedSpec, mapping: dict[str, str],
              ingested_at: str | None = None) -> LoadedFeed:
    """Read one feed, rename `{canonical: source}` columns and attach lineage.

    Values stay strings (empty -> NaN); ``parse_numeric`` converts numeric columns later.

    `source_row` is the 1-based data row (header excluded).
    """
    path = Path(folder) / spec.path
    try:
        reader = READERS[spec.format]
    except KeyError:
        raise ValueError(f"{name}: unsupported format {spec.format!r}") from None
    raw = reader(path, spec)
    frame = raw.rename(columns={source: canonical for canonical, source in mapping.items()})
    frame = frame.replace("", np.nan)
    frame[SOURCE_FILE] = spec.path
    frame[SOURCE_ROW] = np.arange(1, len(frame) + 1)
    frame[ROW_HASH] = _row_hashes(raw)
    frame[INGESTED_AT] = ingested_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    return LoadedFeed(name, path, file_sha256(path), frame)


def parse_dates(values: pd.Series, date_format: str | None = None, utc: bool = False) -> pd.Series:
    """ISO 8601 always parses; `date_format` (strftime, e.g. ``config.date_format(feed)``) is tried
    for the rest. Failures are NaT. `utc` converts aware values to UTC and reads naive ones as UTC."""
    parsed = pd.to_datetime(values, format="ISO8601", errors="coerce", utc=utc)
    if date_format:
        parsed = parsed.fillna(pd.to_datetime(values, format=date_format, errors="coerce", utc=utc))
    return parsed


def parse_numeric(name: str, frame: pd.DataFrame) -> pd.DataFrame:
    """Parse the feed's numeric columns; unparseable values become NaN (the parse check reports them first)."""
    frame = frame.copy()
    for column in NUMERIC_COLUMNS.get(name, ()):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame
