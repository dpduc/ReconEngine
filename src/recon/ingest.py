"""Load raw feeds into the canonical schema with row-level lineage."""

from __future__ import annotations

import hashlib
import io
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from recon.config import FeedSpec
from recon.schema import INGESTED_AT, NUMERIC_COLUMNS, ROW_HASH, SOURCE_FILE, SOURCE_ROW


# Where feed files come from: a folder, or `{relative file name: bytes}` held in memory
# (e.g. browser uploads). Config paths resolve against either the same way.
Sources = str | Path | Mapping[str, bytes]


def read_source(sources: Sources, path: str) -> bytes:
    """The raw bytes of `path` (relative, as written in the run config) within `sources`."""
    if isinstance(sources, Mapping):
        try:
            return sources[path]
        except KeyError:
            raise FileNotFoundError(f"{path}: not among the in-memory files ({', '.join(sorted(sources))})") from None
    return (Path(sources) / path).read_bytes()


def describe_sources(sources: Sources) -> str:
    """Human-readable label for `sources`: the folder, or the in-memory file names."""
    return f"in-memory files: {', '.join(sorted(sources))}" if isinstance(sources, Mapping) else str(sources)


# Format adapters: each returns every cell as a string ("" for empty) so row
# hashes are taken over the raw values, before any parsing.
Reader = Callable[[bytes, FeedSpec], pd.DataFrame]


ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")  # latin-1 decodes any byte: last resort
DELIMITERS = (",", ";", "\t", "|")


def _bytes(data: bytes | str | Path) -> bytes:
    return data if isinstance(data, bytes) else Path(data).read_bytes()


def detect_encoding(data: bytes | str | Path, declared: str | None = None) -> str:
    """`declared` if the content decodes with it, else the first of ENCODINGS that does."""
    data = _bytes(data)
    for encoding in ((declared,) if declared else ()) + ENCODINGS:
        try:
            data.decode(encoding)
        except UnicodeDecodeError:
            continue
        return "utf-8-sig" if encoding == "utf-8" and data.startswith(b"\xef\xbb\xbf") else encoding
    raise AssertionError("unreachable: latin-1 decodes any byte")


def detect_delimiter(data: bytes | str | Path, encoding: str) -> str:
    """The candidate splitting the header and first data lines into the same, largest field count."""
    handle = io.StringIO(_bytes(data).decode(encoding), newline="")
    lines = [handle.readline() for _ in range(2)]
    best, best_fields = ",", 1
    for delimiter in DELIMITERS:
        fields = [line.count(delimiter) + 1 for line in lines if line.strip()]
        if fields and len(set(fields)) == 1 and fields[0] > best_fields:
            best, best_fields = delimiter, fields[0]
    return best


def _read_csv(data: bytes, spec: FeedSpec) -> pd.DataFrame:
    encoding = detect_encoding(data, spec.encoding)
    delimiter = spec.delimiter or detect_delimiter(data, encoding)
    return pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding=encoding, sep=delimiter)


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


def _read_xlsx(data: bytes, spec: FeedSpec) -> pd.DataFrame:
    return _as_text(pd.read_excel(io.BytesIO(data), dtype=object, keep_default_na=False))


def _read_parquet(data: bytes, spec: FeedSpec) -> pd.DataFrame:
    return _as_text(pd.read_parquet(io.BytesIO(data)))


READERS: dict[str, Reader] = {"csv": _read_csv, "xlsx": _read_xlsx, "parquet": _read_parquet}


@dataclass(frozen=True)
class LoadedFeed:
    name: str
    path: str  # as written in the run config, relative to the sources
    sha256: str
    frame: pd.DataFrame  # canonical columns + unmapped pass-through columns + lineage


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _row_hashes(raw: pd.DataFrame) -> list[str]:
    return [
        hashlib.sha256("\x1f".join(row).encode("utf-8")).hexdigest()
        for row in raw.itertuples(index=False, name=None)
    ]


def load_feed(name: str, sources: Sources, spec: FeedSpec, mapping: dict[str, str],
              ingested_at: str | None = None) -> LoadedFeed:
    """Read one feed, rename `{canonical: source}` columns and attach lineage.

    Values stay strings (empty -> NaN); ``parse_numeric`` converts numeric columns later.

    `source_row` is the 1-based data row (header excluded).
    """
    try:
        reader = READERS[spec.format]
    except KeyError:
        raise ValueError(f"{name}: unsupported format {spec.format!r}") from None
    data = read_source(sources, spec.path)
    raw = reader(data, spec)
    frame = raw.rename(columns={source: canonical for canonical, source in mapping.items()})
    frame = frame.replace("", np.nan)
    frame[SOURCE_FILE] = spec.path
    frame[SOURCE_ROW] = np.arange(1, len(frame) + 1)
    frame[ROW_HASH] = _row_hashes(raw)
    frame[INGESTED_AT] = ingested_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    return LoadedFeed(name, spec.path, sha256(data), frame)


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
