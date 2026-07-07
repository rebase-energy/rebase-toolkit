"""Canonical energy time-series layout for warehouse data sources.

Mirrors rebase's open data model (`timedatamodel`/`energydatamodel`, persisted by
`timedb`/`energydb`): an append-only ``series_values`` table with three time axes plus a
``series`` catalog. The same layout — identical column names and semantics — is used in
rebase's ClickHouse store, so data written through a warehouse connector is structurally
interchangeable with platform data.

Time axes (the "multiple time index"):

========================  =====================================================
``valid_time``            the timestamp the observation is about (always present)
``knowledge_time``        when the value became knowable / was issued (forecast
                          issue axis; horizon = valid_time - knowledge_time)
``change_time``           when the row was written or corrected (audit axis;
                          corrections are new rows, never updates)
``valid_time_end``        optional interval end (sentinel 2200-01-01)
========================  =====================================================

Shapes follow ``timedatamodel.DataShape``: SIMPLE (valid_time+value) and VERSIONED
(knowledge_time+valid_time+value) are writable; AUDIT and CORRECTED come back from reads.

A series is keyed like energydb: ``(path, data_type, name)`` — e.g.
``("portfolio/site-1/t01", "forecast", "electricity.supply")`` — with unit, resolution,
timeseries_type (FLAT/OVERLAPPING) and retention as catalog metadata. Because warehouses
lack identity columns, ``series_id`` is a deterministic 63-bit hash of that key, so
registration is idempotent and reads never need a catalog lookup.
"""

from __future__ import annotations

import hashlib
import uuid
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rebase.sources.base import DataSourceError, Frame

VALID_TIME_END_SENTINEL = datetime(2200, 1, 1, tzinfo=UTC)

#: Insert column order of the values table — identical to timedb's ClickHouse layout.
SERIES_VALUES_COLUMNS = (
    "series_id",
    "valid_time",
    "knowledge_time",
    "change_time",
    "value",
    "valid_time_end",
    "run_id",
    "changed_by",
    "annotation",
    "retention",
)

SERIES_CATALOG_COLUMNS = (
    "series_id",
    "path",
    "data_type",
    "name",
    "canonical_unit",
    "timeseries_type",
    "retention",
    "description",
    "inserted_at",
)

RETENTION_TIERS = ("short", "medium", "long", "forever")
TIMESERIES_TYPES = ("FLAT", "OVERLAPPING")


@dataclass(frozen=True)
class SeriesKey:
    """energydb-style series identity: owner path + data type + dotted metric name."""

    path: str
    data_type: str
    name: str

    def __post_init__(self) -> None:
        for field_name in ("path", "data_type", "name"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise DataSourceError(f"series {field_name} must be a non-empty string")

    @property
    def series_id(self) -> int:
        """Deterministic 63-bit id — idempotent registration, no catalog lookup on read."""
        token = "\x1f".join((self.path, self.data_type, self.name)).encode("utf-8")
        return int.from_bytes(hashlib.sha256(token).digest()[:8], "big") & ((1 << 63) - 1)


def new_run_id() -> int:
    """A random 63-bit run id for one write batch (mirrors timedb's per-batch run_id)."""
    return uuid.uuid4().int >> 65


def _utc(ts: Any, *, column: str) -> Any:
    import pandas as pd

    series = pd.to_datetime(ts)
    if getattr(series.dt, "tz", None) is None:
        warnings.warn(f"{column} is timezone-naive; assuming UTC", stacklevel=3)
        series = series.dt.tz_localize("UTC")
    else:
        series = series.dt.tz_convert("UTC")
    return series.astype("datetime64[us, UTC]")


def normalize_series_frame(data: Any) -> Frame:
    """Normalize input into a long frame with valid_time / knowledge_time / value columns.

    Accepts, mirroring ``timedatamodel.TimeSeries.from_pandas``:

    - a ``TimeSeries`` (anything with ``to_pandas()``) — SIMPLE or VERSIONED only,
    - a pandas frame indexed per the shape contract (``valid_time`` or
      ``(knowledge_time, valid_time)``),
    - a pandas frame with those as plain columns.

    AUDIT/CORRECTED shapes are read-only results and rejected on write, exactly like
    ``TimeSeries.validate_for_insert``.
    """
    import pandas as pd

    if hasattr(data, "to_pandas") and not isinstance(data, pd.DataFrame):
        data = data.to_pandas()
    if isinstance(data, pd.Series):
        data = data.rename("value").to_frame()
    if not isinstance(data, pd.DataFrame):
        raise DataSourceError(f"cannot write {type(data)!r}; pass a TimeSeries or a pandas DataFrame")

    df = data.reset_index() if data.index.name or data.index.nlevels > 1 else data.copy()
    df.columns = [str(column) for column in df.columns]
    if "index" in df.columns and "valid_time" not in df.columns:
        df = df.rename(columns={"index": "valid_time"})

    if "change_time" in df.columns:
        raise DataSourceError("AUDIT/CORRECTED shapes are read-only; only SIMPLE and VERSIONED can be written")
    missing = {"valid_time", "value"} - set(df.columns)
    if missing:
        raise DataSourceError(f"series frame is missing required columns: {sorted(missing)}")

    df["valid_time"] = _utc(df["valid_time"], column="valid_time")
    if "knowledge_time" in df.columns:
        df["knowledge_time"] = _utc(df["knowledge_time"], column="knowledge_time")
    if "valid_time_end" in df.columns:
        df["valid_time_end"] = _utc(df["valid_time_end"], column="valid_time_end")
    df["value"] = df["value"].astype("float64")
    return df


def build_values_rows(
    data: Any,
    key: SeriesKey,
    *,
    retention: str = "forever",
    changed_by: str = "",
    annotation: str = "",
    run_id: int | None = None,
) -> Frame:
    """Expand a normalized series frame into full ``series_values`` insert rows.

    Stamps ``knowledge_time`` (batch now, if absent — SIMPLE shape), ``change_time``
    (batch now, always: corrections are new rows) and one ``run_id`` per batch, matching
    timedb's write defaults.
    """
    import pandas as pd

    if retention not in RETENTION_TIERS:
        raise DataSourceError(f"retention must be one of {RETENTION_TIERS}")
    df = normalize_series_frame(data)
    now = pd.Timestamp(datetime.now(UTC)).as_unit("us")

    out = pd.DataFrame(
        {
            "series_id": key.series_id,
            "valid_time": df["valid_time"],
            "knowledge_time": df["knowledge_time"] if "knowledge_time" in df.columns else now,
            "change_time": now,
            "value": df["value"],
            "valid_time_end": df["valid_time_end"]
            if "valid_time_end" in df.columns
            else pd.Timestamp(VALID_TIME_END_SENTINEL).as_unit("us"),
            "run_id": run_id if run_id is not None else new_run_id(),
            "changed_by": changed_by,
            "annotation": annotation,
            "retention": retention,
        }
    )
    return out[list(SERIES_VALUES_COLUMNS)]


def series_values_select(
    table: str,
    series_ids: list[int],
    *,
    start_valid: datetime | None = None,
    end_valid: datetime | None = None,
    as_of: datetime | None = None,
    overlapping: bool = False,
    include_updates: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Build the point-in-time SELECT over ``series_values`` (standard SQL / QUALIFY).

    Replicates timedb's read semantics:

    - default (latest): one row per (series_id, valid_time), winner = greatest
      (knowledge_time, change_time) — latest issue, latest correction within it,
    - ``overlapping=True``: one row per (series_id, valid_time, knowledge_time),
      keeping every forecast issue (latest correction within each),
    - ``as_of``: only rows knowable at that moment (knowledge_time <= as_of),
    - ``include_updates=True``: the full AUDIT shape with changed_by/annotation.

    ``QUALIFY`` is supported by BigQuery, Snowflake, and Databricks, so this builder is
    shared across warehouse connectors. Timestamps are bound as named parameters.
    """
    if not series_ids:
        raise DataSourceError("series_values_select needs at least one series id")
    id_list = ", ".join(str(int(series_id)) for series_id in series_ids)
    where = [f"series_id IN ({id_list})"]
    params: dict[str, Any] = {}
    if start_valid is not None:
        where.append("valid_time >= @start_valid")
        params["start_valid"] = start_valid
    if end_valid is not None:
        where.append("valid_time < @end_valid")
        params["end_valid"] = end_valid
    if as_of is not None:
        where.append("knowledge_time <= @as_of")
        params["as_of"] = as_of

    if include_updates:
        columns = "series_id, valid_time, knowledge_time, change_time, value, changed_by, annotation"
        order = "series_id, valid_time, knowledge_time, change_time"
        sql = f"SELECT {columns} FROM {table} WHERE {' AND '.join(where)} ORDER BY {order}"  # noqa: S608
        return sql, params

    partition = "series_id, valid_time, knowledge_time" if overlapping else "series_id, valid_time"
    order_within = "change_time DESC" if overlapping else "knowledge_time DESC, change_time DESC"
    columns = "series_id, valid_time, knowledge_time, value" if overlapping else "series_id, valid_time, value"
    sql = (
        f"SELECT {columns} FROM {table} WHERE {' AND '.join(where)} "  # noqa: S608
        f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {partition} ORDER BY {order_within}) = 1 "
        f"ORDER BY series_id, valid_time"
    )
    return sql, params
