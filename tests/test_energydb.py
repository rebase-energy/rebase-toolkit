import importlib.util
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from rebase.sources.energy import SeriesKey
from rebase.sources.energydb import EnergyDBStore, _months_in_range

_HAS_PANDAS = importlib.util.find_spec("pandas") is not None
pandas_only = pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")

KEY = SeriesKey("portfolio/site-1/t01", "forecast", "electricity.supply")


class _FakeBucket:
    """In-memory stand-in for rb.Bucket, recording fetches so pruning can be asserted.

    Implements only the subset the store uses. The repo's convention is hand-written fakes,
    never unittest.mock.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fetched: list[str] = []

    def put(self, key, data, *, content_type=None):
        self.objects[key] = data if isinstance(data, bytes) else str(data).encode("utf-8")
        return key

    def get(self, key):
        self.fetched.append(key)
        return self.objects[key]

    def exists(self, key):
        return key in self.objects

    def iter_all(self, prefix=""):
        for key in sorted(self.objects):
            if key.startswith(prefix):
                yield SimpleNamespace(key=key)

    def delete(self, key):
        self.objects.pop(key, None)


def _store():
    bucket = _FakeBucket()
    return EnergyDBStore(bucket), bucket


def test_months_in_range_covers_the_boundary() -> None:
    start = datetime(2026, 7, 30, tzinfo=UTC)
    end = datetime(2026, 9, 2, tzinfo=UTC)
    assert _months_in_range(start, end) == ["2026-07", "2026-08", "2026-09"]


def test_months_in_range_single_month() -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    end = datetime(2026, 7, 31, tzinfo=UTC)
    assert _months_in_range(start, end) == ["2026-07"]


def test_months_in_range_open_ended_is_none() -> None:
    assert _months_in_range(None, None) is None
    assert _months_in_range(datetime(2026, 7, 1, tzinfo=UTC), None) is None


def test_register_series_is_idempotent() -> None:
    store, bucket = _store()
    first = store.register_series(KEY, unit="MW")
    second = store.register_series(KEY, unit="MW")
    assert first == second == KEY
    keys = [key for key in bucket.objects if "/catalog/" in key]
    assert len(keys) == 1
    record = json.loads(bucket.objects[keys[0]])
    assert record["path"] == KEY.path
    assert record["canonical_unit"] == "MW"
    assert record["timeseries_type"] == "FLAT"
    assert record["series_id"] == KEY.series_id


def test_register_series_validates_timeseries_type() -> None:
    store, _bucket = _store()
    with pytest.raises(Exception, match="timeseries_type"):
        store.register_series(KEY, timeseries_type="SLANTED")


def test_register_series_validates_retention() -> None:
    store, _bucket = _store()
    with pytest.raises(Exception, match="retention"):
        store.register_series(KEY, retention="eternal")


@pandas_only
def test_parquet_round_trip_preserves_dtypes() -> None:
    import pandas as pd

    from rebase.sources.energy import build_values_rows
    from rebase.sources.energydb import _decode_parquet, _encode_parquet

    frame = pd.DataFrame({"valid_time": pd.to_datetime(["2026-01-01T00:00Z"]), "value": [1.5]})
    rows = build_values_rows(frame, KEY)
    back = _decode_parquet(_encode_parquet(rows))
    assert list(back.columns) == list(rows.columns)
    assert str(back["valid_time"].dtype) == str(rows["valid_time"].dtype)
    assert str(back["value"].dtype) == "float64"
    assert str(back["run_id"].dtype) == "int64"
    assert back["value"].iloc[0] == 1.5


def _write_raw(store, bucket, key, rows, *, month=None):
    """Put one parquet object directly, bypassing write_series."""
    import pandas as pd

    from rebase.sources.energy import SERIES_VALUES_COLUMNS
    from rebase.sources.energydb import _encode_parquet, _object_key

    records = [
        {
            "series_id": key.series_id,
            "valid_time": pd.Timestamp(valid_time),
            "knowledge_time": pd.Timestamp(knowledge_time),
            "change_time": pd.Timestamp(change_time),
            "value": value,
            "valid_time_end": pd.Timestamp("2200-01-01T00:00Z"),
            "run_id": 1,
            "changed_by": "",
            "annotation": "",
            "retention": "forever",
        }
        for valid_time, knowledge_time, change_time, value in rows
    ]
    frame = pd.DataFrame.from_records(records, columns=list(SERIES_VALUES_COLUMNS))
    partition = month or pd.Timestamp(rows[0][0]).strftime("%Y-%m")
    object_key = _object_key(store.prefix, key.series_id, partition, frame["change_time"].iloc[0], 1)
    bucket.put(object_key, _encode_parquet(frame))
    return object_key


@pandas_only
def test_read_series_projection_and_winner() -> None:
    store, bucket = _store()
    _write_raw(
        store,
        bucket,
        KEY,
        [
            ("2026-01-01T00:00Z", "2026-01-01T06:00Z", "2026-01-01T06:00Z", 1.0),
            ("2026-01-01T00:00Z", "2026-01-01T09:00Z", "2026-01-01T09:00Z", 2.0),
        ],
    )
    out = store.read_series(KEY)
    assert list(out.columns) == ["path", "data_type", "name", "valid_time", "value"]
    assert list(out["value"]) == [2.0]
    assert out["path"].iloc[0] == KEY.path
    assert "series_id" not in out.columns


@pandas_only
def test_read_series_overlapping_projection() -> None:
    store, bucket = _store()
    _write_raw(
        store,
        bucket,
        KEY,
        [
            ("2026-01-01T00:00Z", "2026-01-01T06:00Z", "2026-01-01T06:00Z", 1.0),
            ("2026-01-01T00:00Z", "2026-01-01T09:00Z", "2026-01-01T09:00Z", 2.0),
        ],
    )
    out = store.read_series(KEY, overlapping=True)
    assert list(out.columns) == ["path", "data_type", "name", "valid_time", "knowledge_time", "value"]
    assert sorted(out["value"]) == [1.0, 2.0]


@pandas_only
def test_read_series_include_updates_projection() -> None:
    store, bucket = _store()
    _write_raw(store, bucket, KEY, [("2026-01-01T00:00Z", "2026-01-01T06:00Z", "2026-01-01T06:00Z", 1.0)])
    out = store.read_series(KEY, include_updates=True)
    assert list(out.columns) == [
        "path",
        "data_type",
        "name",
        "valid_time",
        "knowledge_time",
        "change_time",
        "value",
        "changed_by",
        "annotation",
    ]


@pandas_only
def test_read_series_prunes_by_month() -> None:
    store, bucket = _store()
    _write_raw(store, bucket, KEY, [("2026-07-15T00:00Z", "2026-07-15T00:00Z", "2026-07-15T00:00Z", 1.0)])
    _write_raw(store, bucket, KEY, [("2026-08-15T00:00Z", "2026-08-15T00:00Z", "2026-08-15T00:00Z", 2.0)])
    bucket.fetched.clear()
    out = store.read_series(
        KEY, start_valid=datetime(2026, 8, 1, tzinfo=UTC), end_valid=datetime(2026, 8, 31, tzinfo=UTC)
    )
    assert list(out["value"]) == [2.0]
    assert all("valid_month=2026-08" in key for key in bucket.fetched), bucket.fetched


@pandas_only
def test_read_series_without_bounds_reads_every_month() -> None:
    store, bucket = _store()
    _write_raw(store, bucket, KEY, [("2026-07-15T00:00Z", "2026-07-15T00:00Z", "2026-07-15T00:00Z", 1.0)])
    _write_raw(store, bucket, KEY, [("2026-08-15T00:00Z", "2026-08-15T00:00Z", "2026-08-15T00:00Z", 2.0)])
    out = store.read_series(KEY)
    assert sorted(out["value"]) == [1.0, 2.0]


@pandas_only
def test_read_series_applies_valid_time_bounds_half_open() -> None:
    store, bucket = _store()
    _write_raw(
        store,
        bucket,
        KEY,
        [
            ("2026-08-01T00:00Z", "2026-08-01T00:00Z", "2026-08-01T00:00Z", 1.0),
            ("2026-08-02T00:00Z", "2026-08-02T00:00Z", "2026-08-02T00:00Z", 2.0),
        ],
    )
    out = store.read_series(
        KEY, start_valid=datetime(2026, 8, 1, tzinfo=UTC), end_valid=datetime(2026, 8, 2, tzinfo=UTC)
    )
    assert list(out["value"]) == [1.0]


@pandas_only
def test_read_series_empty_returns_the_projection_columns() -> None:
    store, _bucket = _store()
    out = store.read_series(KEY)
    assert list(out.columns) == ["path", "data_type", "name", "valid_time", "value"]
    assert len(out) == 0


@pandas_only
def test_read_series_defaults_as_of_to_the_replay_bound(monkeypatch) -> None:
    store, bucket = _store()
    _write_raw(
        store,
        bucket,
        KEY,
        [
            ("2026-01-01T00:00Z", "2026-01-01T06:00Z", "2026-01-01T06:00Z", 1.0),
            ("2026-01-01T00:00Z", "2026-01-01T09:00Z", "2026-01-01T09:00Z", 2.0),
        ],
    )
    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", "2026-01-01T07:00:00+00:00")
    assert list(store.read_series(KEY)["value"]) == [1.0]
