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
from enum import Enum
from typing import Any

from rebase.sources.base import DataSourceError, Frame, _replay_knowledge_time, _resolve_now

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
    knowledge_time: Any | None = None,
) -> Frame:
    """Expand a normalized series frame into full ``series_values`` insert rows.

    Stamps ``knowledge_time`` (from the frame if present, else ``knowledge_time=``, else the
    batch clock — SIMPLE shape), ``change_time`` (batch clock, always: corrections are new
    rows) and one ``run_id`` per batch, matching timedb's write defaults.

    The batch clock is :func:`_resolve_now`, not wall-clock, so a replay stamps the replay's
    knowledge-time bound. Stamping wall-clock here would record when you *fetched* rather than
    when the data became knowable, which is the one signal that distinguishes a genuine
    upstream correction from a re-fetch of unchanged data.
    """
    import pandas as pd

    if retention not in RETENTION_TIERS:
        raise DataSourceError(f"retention must be one of {RETENTION_TIERS}")
    df = normalize_series_frame(data)
    now = pd.Timestamp(_resolve_now()).as_unit("us")
    if knowledge_time is not None:
        declared = pd.Timestamp(knowledge_time)
        if declared.tz is None:
            warnings.warn("knowledge_time is timezone-naive; assuming UTC", stacklevel=2)
            declared = declared.tz_localize("UTC")
        else:
            declared = declared.tz_convert("UTC")
        declared = declared.as_unit("us")
    else:
        declared = now

    out = pd.DataFrame(
        {
            "series_id": key.series_id,
            "valid_time": df["valid_time"],
            "knowledge_time": df["knowledge_time"] if "knowledge_time" in df.columns else declared,
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

    During a replay run, an ``as_of`` the caller did not pass defaults to the replay's
    knowledge-time bound, so point-in-time reads reproduce what the original run saw.
    """
    if not series_ids:
        raise DataSourceError("series_values_select needs at least one series id")
    if as_of is None:
        as_of = _replay_knowledge_time()
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


_AUDIT_COLUMNS = ["series_id", "valid_time", "knowledge_time", "change_time", "value", "changed_by", "annotation"]


def _winner_rows(df: Frame, *, partition: list[str], order: list[str]) -> Frame:
    """One row per ``partition``, the greatest by ``order``.

    Sorting ascending and taking the tail is the pandas spelling of the SQL builder's
    ``ROW_NUMBER() OVER (PARTITION BY … ORDER BY … DESC) = 1``.
    """
    if not len(df):
        return df
    ranked = df.sort_values(partition + order, kind="stable")
    return ranked.groupby(partition, as_index=False, sort=False).tail(1)


def select_series_winners(
    df: Frame,
    *,
    overlapping: bool = False,
    include_updates: bool = False,
    as_of: datetime | None = None,
) -> Frame:
    """Pick the winning rows from raw ``series_values`` rows, in pandas.

    Mirrors :func:`series_values_select` clause for clause, for a backing that cannot run
    SQL. Both answer to the same definition of "winner"; only the execution differs — so a
    change to one is a change to both, and the parity test in the suite exists to catch a
    drift between them.
    """
    import pandas as pd

    if as_of is None:
        as_of = _replay_knowledge_time()
    out = df
    if as_of is not None and len(out):
        bound = pd.Timestamp(as_of)
        if bound.tz is None:
            bound = bound.tz_localize("UTC")
        out = out[out["knowledge_time"] <= bound]

    if include_updates:
        ordered = out[_AUDIT_COLUMNS]
        if len(ordered):
            ordered = ordered.sort_values(["series_id", "valid_time", "knowledge_time", "change_time"], kind="stable")
        return ordered.reset_index(drop=True)

    partition = ["series_id", "valid_time", "knowledge_time"] if overlapping else ["series_id", "valid_time"]
    order = ["change_time"] if overlapping else ["knowledge_time", "change_time"]
    winners = _winner_rows(out, partition=partition, order=order)
    columns = (
        ["series_id", "valid_time", "knowledge_time", "value"] if overlapping else ["series_id", "valid_time", "value"]
    )
    winners = winners[columns]
    if len(winners):
        winners = winners.sort_values(["series_id", "valid_time"], kind="stable")
    return winners.reset_index(drop=True)


def series_keys(keys: Any) -> list[SeriesKey]:
    """Normalise one or many series keys into a list of :class:`SeriesKey`."""
    if isinstance(keys, (SeriesKey, tuple, str)) or not hasattr(keys, "__iter__"):
        keys = [keys]
    resolved: list[SeriesKey] = []
    for key in keys:
        if isinstance(key, SeriesKey):
            resolved.append(key)
        elif isinstance(key, tuple) and len(key) == 3:
            resolved.append(SeriesKey(path=key[0], data_type=key[1], name=key[2]))
        else:
            raise DataSourceError(f"series keys must be SeriesKey or (path, data_type, name), got {key!r}")
    if not resolved:
        raise DataSourceError("read_series needs at least one series key")
    return resolved


def attach_series_keys(df: Frame, by_id: dict[int, SeriesKey]) -> Frame:
    """Swap the raw ``series_id`` column for path/data_type/name — ids are never exposed."""
    missing = set(df["series_id"]) - set(by_id)
    if missing:
        raise DataSourceError(f"by_id must cover every series_id in the frame; missing {sorted(missing)!r}")
    out = df.copy()
    out.insert(0, "name", out["series_id"].map(lambda sid: by_id[sid].name))
    out.insert(0, "data_type", out["series_id"].map(lambda sid: by_id[sid].data_type))
    out.insert(0, "path", out["series_id"].map(lambda sid: by_id[sid].path))
    return out.drop(columns=["series_id"])


MAX_SAMPLE_VALID_TIMES = 10


class OnNull(Enum):
    """What a write does when the incoming value is null and a value is already stored."""

    KEEP_STORED = "keep_stored"
    """Never replace a stored real value with a null — a transient upstream gap must not destroy data."""

    WRITE_NULL = "write_null"
    """Treat the null as a real observation and record the gap."""


def _is_missing(value: Any) -> bool:
    """True for any null-like scalar: ``None``, float ``NaN``, ``pandas.NA``, or ``NaT``.

    Prefers ``pandas.isna`` because it recognises the nullable-dtype sentinels (``pd.NA``,
    ``pd.NaT``) uniformly with plain ``None``/``NaN``, where a bare ``is None`` / ``math.isnan``
    check misses them — ``pd.NA`` is neither ``None`` nor a ``float``, so it fell through as a
    "real" value and ``==`` on it raised ``TypeError: boolean value of NA is ambiguous``.
    Falls back to the ``None``/``NaN`` check when pandas is not installed, since
    :meth:`Change.values_equal` must keep working without it.
    """
    try:
        import pandas as pd
    except ImportError:
        import math

        return value is None or (isinstance(value, float) and math.isnan(value))
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        # pd.isna returns an array for array-likes; a scalar value can't be missing that way.
        return False


class Change:
    """How a write decides whether an incoming value is unchanged.

    ``exact()`` is the default because over-suppression is the failure mode this vocabulary
    exists to prevent: a tolerance that silently discards real corrections defeats the point
    of re-fetching. ``tolerance()`` takes an **absolute** bound and there is deliberately no
    relative variant — a relative band is the recorded defect, because on a 5,000-magnitude
    series a 1e-4 relative band ignores every correction under 0.5.
    """

    __slots__ = ("_atol",)

    def __init__(self, *, atol: float | None = None) -> None:
        self._atol = atol

    @classmethod
    def exact(cls) -> Change:
        """Any difference is a change. Suppresses only true no-op rewrites."""
        return cls()

    @classmethod
    def tolerance(cls, atol: float) -> Change:
        """Treat differences within ``atol`` (absolute) as unchanged. ``atol`` must be positive."""
        if isinstance(atol, bool) or not isinstance(atol, (int, float)):
            raise DataSourceError("Change.tolerance requires a number")
        if atol <= 0:
            raise DataSourceError("Change.tolerance requires atol > 0; use Change.exact() for no tolerance")
        return cls(atol=float(atol))

    def values_equal(self, new: Any, old: Any) -> bool:
        """True when the two values count as unchanged. Any null equals any null, matching timedb."""
        new_missing = _is_missing(new)
        old_missing = _is_missing(old)
        if new_missing or old_missing:
            return new_missing and old_missing
        if self._atol is None:
            return bool(new == old)
        return bool(abs(float(new) - float(old)) <= self._atol)


@dataclass(frozen=True)
class SeriesWriteResult:
    """Outcome of one :meth:`EnergyDBStore.write_series` call, including what it declined.

    Suppression counts are returned rather than logged-and-forgotten because an undeclared,
    unreported threshold is exactly what made the original defect invisible until someone
    went looking.
    """

    series: SeriesKey
    rows_written: int
    objects_written: tuple[str, ...] = ()
    suppressed_unchanged: int = 0
    suppressed_null: int = 0
    sample_valid_times: tuple[str, ...] = ()
    fail_open: bool = False


def select_current_state(df: Frame, *, overlapping: bool = False, as_of: datetime | None = None) -> Frame:
    """Winner rows with ``annotation`` and ``changed_by`` kept, for change detection.

    The public read projection drops both, but equality compares them, so the write path
    needs its own shape. Uses the same ranking as :func:`select_series_winners`, so the two
    agree on which row wins.
    """
    import pandas as pd

    columns = ["series_id", "valid_time", "value", "annotation", "changed_by"]
    if overlapping:
        columns.insert(2, "knowledge_time")
    if not len(df):
        return pd.DataFrame(columns=columns)
    out = df
    if as_of is None:
        as_of = _replay_knowledge_time()
    if as_of is not None:
        bound = pd.Timestamp(as_of)
        if bound.tz is None:
            bound = bound.tz_localize("UTC")
        out = out[out["knowledge_time"] <= bound]
    partition = ["series_id", "valid_time", "knowledge_time"] if overlapping else ["series_id", "valid_time"]
    order = ["change_time"] if overlapping else ["knowledge_time", "change_time"]
    winners = _winner_rows(out, partition=partition, order=order)
    return winners[columns].reset_index(drop=True)


def suppress_rows(
    batch: Frame,
    stored: Frame,
    *,
    on_null: OnNull = OnNull.KEEP_STORED,
    skip_unchanged: bool = False,
    change: Change | None = None,
    overlapping: bool = False,
) -> tuple[Frame, dict[str, Any]]:
    """Drop the rows of ``batch`` that the declared semantics say not to write.

    Seven rules, evaluated per row against the stored winner:

    1. nothing stored -> write (a new point)
    2. stored null, incoming null -> skip (nothing to say)
    3. stored null, incoming real -> write (the gap fill — backfill's whole purpose)
    4. stored real, incoming null -> ``on_null`` decides
    5. equal value, annotation and changed_by -> skip (genuinely unchanged)
    6. equal value, differing annotation or changed_by -> write (still meaningful)
    7. differing value -> write (the revision)

    Rules 1-4 always apply; rules 5-7 only when ``skip_unchanged`` is set. An ``OVERLAPPING``
    series bypasses all of it: every publication of a forecast is meaningful, so a
    republication at a new knowledge_time with an unchanged value is a genuine observation
    and suppressing it loses information no later read can recover.
    """
    empty_report: dict[str, Any] = {"suppressed_unchanged": 0, "suppressed_null": 0, "sample_valid_times": ()}
    if overlapping or not len(batch):
        # The store also skips the stored-state read for OVERLAPPING series, so in production
        # this branch is never reached. It stays as defence in depth: a future caller that
        # forgets the outer gate still cannot silently drop a forecast republication.
        return batch, empty_report

    comparer = change or Change.exact()
    lookup: dict[Any, tuple[Any, Any, Any]] = {}
    if len(stored):
        for row in stored.itertuples(index=False):
            lookup[row.valid_time] = (row.value, row.annotation, row.changed_by)

    keep: list[bool] = []
    suppressed_unchanged = 0
    suppressed_null = 0
    samples: list[str] = []

    def _metadata_equal(new_meta: Any, old_meta: Any) -> bool:
        # Any null and the empty string all count as "no annotation" — otherwise an all-null
        # metadata column reads as "differs" on every row and skip_unchanged never suppresses.
        new_norm = "" if _is_missing(new_meta) else new_meta
        old_norm = "" if _is_missing(old_meta) else old_meta
        return bool(new_norm == old_norm)

    for row in batch.itertuples(index=False):
        current = lookup.get(row.valid_time)
        if current is None:
            keep.append(True)  # rule 1
            continue
        old_value, old_annotation, old_changed_by = current
        if _is_missing(row.value):
            if _is_missing(old_value):
                decision, bucket = False, "null"  # rule 2
            elif on_null is OnNull.KEEP_STORED:
                decision, bucket = False, "null"  # rule 4, keep
            else:
                decision, bucket = True, ""  # rule 4, write
        elif _is_missing(old_value):
            decision, bucket = True, ""  # rule 3
        elif not skip_unchanged:
            decision, bucket = True, ""  # rules 5-7 disabled
        elif not comparer.values_equal(row.value, old_value):
            decision, bucket = True, ""  # rule 7
        elif not _metadata_equal(row.annotation, old_annotation) or not _metadata_equal(row.changed_by, old_changed_by):
            decision, bucket = True, ""  # rule 6
        else:
            decision, bucket = False, "unchanged"  # rule 5
        keep.append(decision)
        if not decision:
            if bucket == "null":
                suppressed_null += 1
            else:
                suppressed_unchanged += 1
            if len(samples) < MAX_SAMPLE_VALID_TIMES:
                moment = row.valid_time
                samples.append(moment.isoformat() if hasattr(moment, "isoformat") else str(moment))

    report = {
        "suppressed_unchanged": suppressed_unchanged,
        "suppressed_null": suppressed_null,
        "sample_valid_times": tuple(samples),
    }
    return batch[keep].reset_index(drop=True), report
