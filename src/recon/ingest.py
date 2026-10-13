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


def _read_csv(path: Path, spec: FeedSpec) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding=spec.encoding)


READERS: dict[str, Reader] = {"csv": _read_csv}


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
    for column in NUMERIC_COLUMNS.get(name, ()):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame[SOURCE_FILE] = spec.path
    frame[SOURCE_ROW] = np.arange(1, len(frame) + 1)
    frame[ROW_HASH] = _row_hashes(raw)
    frame[INGESTED_AT] = ingested_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    return LoadedFeed(name, path, file_sha256(path), frame)
