"""Rebase's canonical EnergyDB time-series layout, persisted to bucket storage.

An append-only object log: every write puts a **new immutable** parquet object; nothing is
ever rewritten. Reads list the relevant prefixes, concatenate, and pick winners in pandas
from the definition shared with :func:`rebase.sources.energy.series_values_select`. That
matches EnergyDB itself, where corrections are new rows and never updates — an immutable
object store is the natural substrate for an append-only log.

.. important::
   **This bucket backing is transitional.** It exists so projects can produce, persist and
   read EnergyDB-shaped time series before the toolkit can reach a real EnergyDB. It is
   intended to be replaced by a proper connector in the mould of ``bigquery`` / ``snowflake``
   / ``databricks`` / ``fabric`` — a :class:`~rebase.sources.base.DataSource` with
   ``read``/``read_bitemporal``/``write``, credentials through ``ConnectorSpec``, and the
   store's own ``skip_unchanged`` doing suppression server-side.

   The seam is this module. Everything bucket-specific — key layout, month pruning, parquet
   IO, the client-side read-before-write — lives here and is expected to be discarded.
   Everything meant to survive lives in :mod:`rebase.sources.energy`: the vocabulary
   (``skip_unchanged``, ``unchanged_scope``, :class:`~rebase.sources.energy.Change`,
   :class:`~rebase.sources.energy.OnNull`), the canonical column layout, and the
   winner-selection semantics. Replacing the backing should mean replacing this file.

   Because objects hold exactly ``SERIES_VALUES_COLUMNS`` in order with parquet-preserved
   dtypes, everything written here can be loaded into a real EnergyDB as-is. That shape
   discipline is the point; protect it against any more convenient on-disk format.

pandas and pyarrow are imported lazily inside functions, so this module stays importable
with the toolkit's core (pandas-free) dependencies. Install ``rebase-toolkit[energydb]``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from rebase.sources.base import DataSourceError, Frame
from rebase.sources.energy import (
    RETENTION_TIERS,
    SERIES_CATALOG_COLUMNS,
    TIMESERIES_TYPES,
    SeriesKey,
    attach_series_keys,
    select_series_winners,
    series_keys,
)

_logger = logging.getLogger("rebase.sources")

_MONTH_FORMAT = "%Y-%m"
_CHANGE_TIME_FORMAT = "%Y%m%dT%H%M%S%f"
_PARQUET_CONTENT_TYPE = "application/vnd.apache.parquet"


def _catalog_key(prefix: str, series_id: int) -> str:
    return f"{prefix}/catalog/{series_id}.json"


def _series_prefix(prefix: str, series_id: int) -> str:
    return f"{prefix}/series/{series_id}/"


def _month_prefix(prefix: str, series_id: int, month: str) -> str:
    return f"{_series_prefix(prefix, series_id)}valid_month={month}/"


def _object_key(prefix: str, series_id: int, month: str, change_time: Any, run_id: int) -> str:
    stamp = change_time.strftime(_CHANGE_TIME_FORMAT)
    return f"{_month_prefix(prefix, series_id, month)}{stamp}Z-{run_id}.parquet"


def _months_in_range(start: datetime | None, end: datetime | None) -> list[str] | None:
    """The ``valid_month`` partitions spanning ``[start, end)``, or ``None`` when unbounded.

    ``None`` means "no pruning possible" — the caller lists the whole series prefix. Both
    bounds are needed, because one open end could reach any month.
    """
    if start is None or end is None:
        return None
    months: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return months


def _encode_parquet(df: Frame) -> bytes:
    import io

    from rebase._optional import optional_module

    optional_module("pyarrow", "energydb")
    buffer = io.BytesIO()
    df.to_parquet(buffer, index=False)
    return buffer.getvalue()


def _decode_parquet(blob: bytes) -> Frame:
    import io

    import pandas as pd

    from rebase._optional import optional_module

    optional_module("pyarrow", "energydb")
    return pd.read_parquet(io.BytesIO(blob))


class EnergyDBStore:
    """Read and write Rebase's canonical time-series layout in a bucket.

    See the module docstring — this backing is transitional, and a real connector is intended.
    """

    def __init__(self, bucket: Any, *, prefix: str = "energydb") -> None:
        if isinstance(bucket, str):
            from rebase.client import Bucket

            bucket = Bucket.from_name(bucket)
        for method in ("put", "get", "iter_all"):
            if not callable(getattr(bucket, method, None)):
                raise DataSourceError(f"energydb bucket must provide a callable {method}()")
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def register_series(
        self,
        key: SeriesKey,
        *,
        unit: str = "dimensionless",
        timeseries_type: str = "FLAT",
        retention: str = "forever",
        description: str | None = None,
    ) -> SeriesKey:
        """Write the catalog record for ``key`` (idempotent) and return it.

        ``series_id`` is derived from the key, so re-registration overwrites an identical
        record and reads never need a catalog lookup. Registering is how a series becomes
        ``OVERLAPPING``: an unregistered series is treated as ``FLAT``.
        """
        if timeseries_type not in TIMESERIES_TYPES:
            raise DataSourceError(f"timeseries_type must be one of {TIMESERIES_TYPES}; got {timeseries_type!r}")
        if retention not in RETENTION_TIERS:
            raise DataSourceError(f"retention must be one of {RETENTION_TIERS}; got {retention!r}")
        record = {
            "series_id": key.series_id,
            "path": key.path,
            "data_type": key.data_type,
            "name": key.name,
            "canonical_unit": unit,
            "timeseries_type": timeseries_type,
            "retention": retention,
            "description": description or "",
            "inserted_at": None,
        }
        if set(record) != set(SERIES_CATALOG_COLUMNS):
            # A real runtime check rather than an assert: asserts vanish under -O, and this
            # guards the property that makes a future load into a real EnergyDB a load.
            mismatch = sorted(set(SERIES_CATALOG_COLUMNS) ^ set(record))
            raise DataSourceError(f"catalog record does not match SERIES_CATALOG_COLUMNS: {mismatch}")
        payload = json.dumps(record, sort_keys=True).encode("utf-8")
        self.bucket.put(_catalog_key(self.prefix, key.series_id), payload, content_type="application/json")
        return key

    def _read_catalog(self, series_id: int) -> dict[str, Any] | None:
        """The catalog record, or ``None`` when the series was never registered."""
        key = _catalog_key(self.prefix, series_id)
        exists = getattr(self.bucket, "exists", None)
        if callable(exists) and not exists(key):
            return None
        try:
            return json.loads(self.bucket.get(key))
        except Exception:  # noqa: BLE001 - an unreadable catalog means "unregistered", not a failure
            return None

    def _is_overlapping(self, series_id: int) -> bool:
        record = self._read_catalog(series_id)
        return bool(record and record.get("timeseries_type") == "OVERLAPPING")

    def _raw_rows(self, series_ids: list[int], months: list[str] | None) -> Frame:
        """Concatenate every stored object for these series, pruned to ``months`` when known."""
        import pandas as pd

        from rebase.sources.energy import SERIES_VALUES_COLUMNS

        frames: list[Frame] = []
        for series_id in series_ids:
            prefixes = (
                [_month_prefix(self.prefix, series_id, month) for month in months]
                if months is not None
                else [_series_prefix(self.prefix, series_id)]
            )
            for prefix in prefixes:
                for entry in self.bucket.iter_all(prefix=prefix):
                    frames.append(_decode_parquet(self.bucket.get(entry.key)))
        if not frames:
            return pd.DataFrame(columns=list(SERIES_VALUES_COLUMNS))
        return pd.concat(frames, ignore_index=True)

    def read_series(
        self,
        keys: Any,
        *,
        start_valid: datetime | None = None,
        end_valid: datetime | None = None,
        as_of: datetime | None = None,
        overlapping: bool = False,
        include_updates: bool = False,
    ) -> Frame:
        """Point-in-time read with EnergyDB semantics, in the shape a real query returns.

        - default: the latest view — one row per ``valid_time`` (latest issue, latest
          correction within it),
        - ``as_of``: only what was knowable then (bounds ``knowledge_time``); defaults to the
          replay bound during a replay,
        - ``overlapping=True``: every forecast issue (adds ``knowledge_time``),
        - ``include_updates=True``: the full AUDIT trail.

        ``keys`` is one ``(path, data_type, name)`` tuple / :class:`SeriesKey` or a list of
        them. Results carry ``path``/``data_type``/``name`` — never raw ids.
        """
        import pandas as pd

        resolved = series_keys(keys)
        by_id = {key.series_id: key for key in resolved}
        months = _months_in_range(start_valid, end_valid)
        raw = self._raw_rows(list(by_id), months)
        if start_valid is not None and len(raw):
            raw = raw[raw["valid_time"] >= pd.Timestamp(start_valid)]
        if end_valid is not None and len(raw):
            raw = raw[raw["valid_time"] < pd.Timestamp(end_valid)]
        winners = select_series_winners(raw, overlapping=overlapping, include_updates=include_updates, as_of=as_of)
        return attach_series_keys(winners, by_id)
