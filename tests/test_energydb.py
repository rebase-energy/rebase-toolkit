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
    from rebase.sources.energydb import _content_digest, _encode_parquet, _object_key

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
    blob = _encode_parquet(frame)
    object_key = _object_key(
        store.prefix, key.series_id, partition, frame["change_time"].iloc[0], 1, _content_digest(blob)
    )
    bucket.put(object_key, blob)
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


def _frame(rows):
    """(valid_time, value) pairs -> a SIMPLE input frame."""
    import pandas as pd

    return pd.DataFrame({"valid_time": pd.to_datetime([row[0] for row in rows]), "value": [row[1] for row in rows]})


@pandas_only
def test_write_series_round_trips() -> None:
    store, _bucket = _store()
    result = store.write_series(_frame([("2026-01-01T00:00Z", 1.5), ("2026-01-01T01:00Z", 2.5)]), KEY)
    assert result.rows_written == 2
    assert len(result.objects_written) == 1
    back = store.read_series(KEY)
    assert list(back["value"]) == [1.5, 2.5]


@pandas_only
def test_write_series_splits_objects_by_month() -> None:
    store, _bucket = _store()
    result = store.write_series(_frame([("2026-07-31T00:00Z", 1.0), ("2026-08-01T00:00Z", 2.0)]), KEY)
    assert len(result.objects_written) == 2
    assert any("valid_month=2026-07" in key for key in result.objects_written)
    assert any("valid_month=2026-08" in key for key in result.objects_written)


@pandas_only
def test_write_series_never_rewrites_an_object() -> None:
    store, bucket = _store()
    store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY, run_id=1)
    first = set(bucket.objects)
    store.write_series(_frame([("2026-01-01T00:00Z", 2.0)]), KEY, run_id=2)
    assert first < set(bucket.objects)  # strictly grown; nothing replaced


@pandas_only
def test_write_series_suppresses_unchanged_and_reports() -> None:
    store, _bucket = _store()
    store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY)
    result = store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY, skip_unchanged=True)
    assert result.rows_written == 0
    assert result.suppressed_unchanged == 1
    assert result.objects_written == ()
    assert len(result.sample_valid_times) == 1


@pandas_only
def test_write_series_writes_a_correction_outside_the_tolerance() -> None:
    from rebase.sources.energy import Change

    store, _bucket = _store()
    store.write_series(_frame([("2026-01-01T00:00Z", 5000.0)]), KEY)
    # A relative band would call this unchanged; an absolute 1e-6 must not.
    result = store.write_series(
        _frame([("2026-01-01T00:00Z", 5000.5)]), KEY, skip_unchanged=True, change=Change.tolerance(1e-6)
    )
    assert result.rows_written == 1
    assert result.suppressed_unchanged == 0
    assert list(store.read_series(KEY)["value"]) == [5000.5]


@pandas_only
def test_write_series_keeps_a_stored_value_against_a_null() -> None:
    store, _bucket = _store()
    store.write_series(_frame([("2026-01-01T00:00Z", 5.0)]), KEY)
    result = store.write_series(_frame([("2026-01-01T00:00Z", float("nan"))]), KEY)
    assert result.rows_written == 0
    assert result.suppressed_null == 1
    assert list(store.read_series(KEY)["value"]) == [5.0]


@pandas_only
def test_write_series_write_null_records_the_gap() -> None:
    from rebase.sources.energy import OnNull

    store, _bucket = _store()
    store.write_series(_frame([("2026-01-01T00:00Z", 5.0)]), KEY)
    result = store.write_series(_frame([("2026-01-01T00:00Z", float("nan"))]), KEY, on_null=OnNull.WRITE_NULL)
    assert result.rows_written == 1


@pandas_only
def test_write_series_overlapping_keeps_identical_republications() -> None:
    import pandas as pd

    store, _bucket = _store()
    store.register_series(KEY, timeseries_type="OVERLAPPING")
    first = pd.DataFrame(
        {
            "valid_time": pd.to_datetime(["2026-01-01T00:00Z"]),
            "knowledge_time": pd.to_datetime(["2026-01-01T06:00Z"]),
            "value": [7.0],
        }
    )
    second = pd.DataFrame(
        {
            "valid_time": pd.to_datetime(["2026-01-01T00:00Z"]),
            "knowledge_time": pd.to_datetime(["2026-01-01T09:00Z"]),
            "value": [7.0],
        }
    )
    store.write_series(first, KEY, skip_unchanged=True)
    result = store.write_series(second, KEY, skip_unchanged=True)
    assert result.rows_written == 1
    assert result.suppressed_unchanged == 0
    assert len(store.read_series(KEY, overlapping=True)) == 2


@pandas_only
def test_write_series_flat_suppresses_what_overlapping_keeps() -> None:
    import pandas as pd

    store, _bucket = _store()  # unregistered -> FLAT
    first = pd.DataFrame(
        {
            "valid_time": pd.to_datetime(["2026-01-01T00:00Z"]),
            "knowledge_time": pd.to_datetime(["2026-01-01T06:00Z"]),
            "value": [7.0],
        }
    )
    second = pd.DataFrame(
        {
            "valid_time": pd.to_datetime(["2026-01-01T00:00Z"]),
            "knowledge_time": pd.to_datetime(["2026-01-01T09:00Z"]),
            "value": [7.0],
        }
    )
    store.write_series(first, KEY, skip_unchanged=True)
    assert store.write_series(second, KEY, skip_unchanged=True).rows_written == 0


@pandas_only
def test_write_series_is_fail_open(monkeypatch, caplog) -> None:
    import importlib
    import logging

    # importlib.import_module resolves the submodule by its fully-qualified name directly,
    # unlike `import rebase.sources.energydb as ...`, which walks attribute access instead and
    # would find `rb.sources.energydb` the factory function now that one is exported there.
    energydb_module = importlib.import_module("rebase.sources.energydb")

    store, _bucket = _store()
    store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY)

    def _boom(*args, **kwargs):
        raise RuntimeError("comparison exploded")

    monkeypatch.setattr(energydb_module, "suppress_rows", _boom)
    with caplog.at_level(logging.WARNING, logger="rebase.sources"):
        result = store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY, skip_unchanged=True)
    assert result.fail_open is True
    assert result.rows_written == 1  # the batch was written unfiltered
    # Fail-open must be visible, not just returned: a warning naming the failure is on the logger.
    assert any("suppression failed" in record.getMessage() for record in caplog.records)
    assert any("comparison exploded" in record.getMessage() for record in caplog.records)


@pandas_only
def test_write_series_skips_the_read_when_no_suppression_is_possible() -> None:
    from rebase.sources.energy import OnNull

    store, bucket = _store()
    store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY)
    bucket.fetched.clear()
    store.write_series(_frame([("2026-01-01T01:00Z", 2.0)]), KEY, skip_unchanged=False, on_null=OnNull.WRITE_NULL)
    assert bucket.fetched == []


@pandas_only
def test_write_series_logs_a_summary_when_it_suppresses(caplog) -> None:
    import logging

    store, _bucket = _store()
    store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY)
    with caplog.at_level(logging.WARNING, logger="rebase.sources"):
        store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY, skip_unchanged=True)
    assert any("suppressed" in record.getMessage() for record in caplog.records)


@pandas_only
def test_write_series_accepts_a_declared_knowledge_time() -> None:
    import pandas as pd

    from rebase.sources.base import KnowledgeTime

    store, _bucket = _store()
    frame = pd.DataFrame(
        {
            "valid_time": pd.to_datetime(["2026-01-01T00:00Z"]),
            "issued_at": pd.to_datetime(["2026-01-01T06:00Z"]),
            "value": [1.0],
        }
    )
    store.write_series(frame, KEY, knowledge_time=KnowledgeTime.from_source("issued_at"))
    out = store.read_series(KEY, overlapping=True)
    assert out["knowledge_time"].iloc[0] == pd.Timestamp("2026-01-01T06:00Z")


@pandas_only
def test_write_series_rejects_an_unknown_unchanged_scope() -> None:
    store, _bucket = _store()
    with pytest.raises(Exception, match="unchanged_scope"):
        store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY, unchanged_scope="sideways")


# --- Fix round 1: content-addressed keys survive a frozen replay clock -----------------------


@pandas_only
def test_write_series_content_addressing_survives_a_frozen_replay_clock(monkeypatch) -> None:
    store, bucket = _store()
    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", "2026-06-01T00:00:00+00:00")
    first = store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY, run_id=42)
    second = store.write_series(_frame([("2026-01-01T00:00Z", 2.0)]), KEY, run_id=42)
    # Same series, same month, same frozen change_time, same caller-supplied run_id: only the
    # content digest can keep these two writes from landing on the same key.
    assert first.objects_written != second.objects_written
    assert len(bucket.objects) == 2
    out = store.read_series(KEY, include_updates=True)
    assert sorted(out["value"]) == [1.0, 2.0]


@pandas_only
def test_write_series_identical_content_under_a_frozen_clock_is_idempotent(monkeypatch) -> None:
    store, bucket = _store()
    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", "2026-06-01T00:00:00+00:00")
    first = store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY, run_id=42)
    second = store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY, run_id=42)
    assert first.objects_written == second.objects_written
    assert len(bucket.objects) == 1


@pandas_only
def test_write_series_first_object_bytes_survive_a_later_differing_write(monkeypatch) -> None:
    store, bucket = _store()
    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", "2026-06-01T00:00:00+00:00")
    first = store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY, run_id=42)
    first_key = first.objects_written[0]
    first_bytes = bucket.objects[first_key]
    store.write_series(_frame([("2026-01-01T00:00Z", 2.0)]), KEY, run_id=42)
    assert bucket.objects[first_key] == first_bytes


# --- Fix round 1: unchanged_scope must not switch off on_null protection ---------------------


@pandas_only
def test_write_series_knowledge_time_scope_still_protects_against_a_null() -> None:
    from rebase.sources.energy import OnNull

    store, _bucket = _store()  # unregistered -> FLAT
    store.write_series(_frame([("2026-01-01T00:00Z", 5.0)]), KEY)
    result = store.write_series(
        _frame([("2026-01-01T00:00Z", float("nan"))]),
        KEY,
        unchanged_scope="knowledge_time",
        on_null=OnNull.KEEP_STORED,
    )
    assert result.rows_written == 0
    assert result.suppressed_null == 1
    assert list(store.read_series(KEY)["value"]) == [5.0]


# --- Fix round 1: on_null must be validated -------------------------------------------------


@pandas_only
def test_write_series_rejects_an_invalid_on_null() -> None:
    store, _bucket = _store()
    with pytest.raises(Exception, match="on_null"):
        store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY, on_null="keep_stored")


# --- Fix round 1: DataSourceError, never a bare AttributeError -------------------------------


@pandas_only
def test_write_series_rejects_a_raw_timestamp_as_knowledge_time() -> None:
    import pandas as pd

    store, _bucket = _store()
    with pytest.raises(Exception, match="KnowledgeTime"):
        store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY, knowledge_time=pd.Timestamp("2026-01-01T00:00Z"))


@pandas_only
def test_write_series_accepts_a_tuple_key() -> None:
    store, _bucket = _store()
    result = store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), (KEY.path, KEY.data_type, KEY.name))
    assert result.rows_written == 1
    assert list(store.read_series(KEY)["value"]) == [1.0]


@pandas_only
def test_write_series_rejects_multiple_keys() -> None:
    store, _bucket = _store()
    other = SeriesKey("portfolio/site-2/t02", "forecast", "electricity.supply")
    with pytest.raises(Exception, match="one series"):
        store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), [KEY, other])


# --- Final fix wave: unchanged_scope="knowledge_time" must actually widen the lookup key -----


def _issue(valid_time, knowledge_time, value):
    import pandas as pd

    return pd.DataFrame(
        {
            "valid_time": pd.to_datetime([valid_time]),
            "knowledge_time": pd.to_datetime([knowledge_time]),
            "value": [value],
        }
    )


@pandas_only
def test_write_series_unchanged_scope_discriminates_between_issues_at_one_valid_time() -> None:
    # Two stored rows at the same valid_time, different knowledge_time, different values. A
    # batch row matching the OLDER issue exactly must be suppressed under scope="knowledge_time"
    # (compared against its own issue) but written under scope="valid_time" (compared against the
    # widest winner, the newer issue, whose value differs) -- so a scope that was silently ignored
    # would make both calls agree, and this test would catch that.
    def _two_issues():
        store, _bucket = _store()  # unregistered -> FLAT
        store.write_series(_issue("2026-01-01T00:00Z", "2026-01-01T05:00Z", 5.0), KEY)
        store.write_series(_issue("2026-01-01T00:00Z", "2026-01-01T09:00Z", 7.0), KEY)
        return store

    batch = _issue("2026-01-01T00:00Z", "2026-01-01T05:00Z", 5.0)  # matches the OLDER issue

    under_valid_time = _two_issues().write_series(batch, KEY, skip_unchanged=True, unchanged_scope="valid_time")
    under_knowledge_time = _two_issues().write_series(batch, KEY, skip_unchanged=True, unchanged_scope="knowledge_time")
    assert under_valid_time.rows_written == 1
    assert under_knowledge_time.rows_written == 0
    assert under_valid_time.rows_written != under_knowledge_time.rows_written


# --- Final fix wave: tz-naive bounds and stored timestamps must not raise or silently miss ---


@pandas_only
def test_read_series_accepts_naive_valid_bounds() -> None:
    store, bucket = _store()
    _write_raw(store, bucket, KEY, [("2026-08-15T00:00Z", "2026-08-15T00:00Z", "2026-08-15T00:00Z", 2.0)])
    out = store.read_series(KEY, start_valid=datetime(2026, 8, 1), end_valid=datetime(2026, 8, 31))
    assert list(out["value"]) == [2.0]


# --- Final fix wave: the catalog get is also skipped when no suppression can occur -----------


@pandas_only
def test_write_series_skips_the_catalog_read_too_when_no_suppression_is_possible() -> None:
    from rebase.sources.energy import OnNull

    store, bucket = _store()
    store.register_series(KEY, timeseries_type="FLAT")
    store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY)
    bucket.fetched.clear()
    store.write_series(_frame([("2026-01-01T01:00Z", 2.0)]), KEY, skip_unchanged=False, on_null=OnNull.WRITE_NULL)
    assert bucket.fetched == []


# --- Final fix wave: the object key grammar is content-addressed, not just "has valid_month=" --


@pandas_only
def test_object_key_matches_the_documented_grammar() -> None:
    import hashlib
    import re

    store, bucket = _store()
    result = store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY, run_id=42)
    (object_key,) = result.objects_written

    prefix = f"{store.prefix}/series/{KEY.series_id}/valid_month=2026-01/"
    assert object_key.startswith(prefix), object_key
    suffix = object_key[len(prefix) :]
    match = re.fullmatch(r"\d{8}T\d{12}Z-42-([0-9a-f]{12})\.parquet", suffix)
    assert match, suffix
    digest = match.group(1)
    assert digest == hashlib.sha256(bucket.objects[object_key]).hexdigest()[:12]


# --- Task 8: factory, exports and packaging --------------------------------------------------


def test_factory_is_exported_and_lazy() -> None:
    import rebase as rb

    assert callable(rb.sources.energydb)
    for name in ("Change", "OnNull", "SeriesWriteResult", "EnergyDBStore"):
        assert hasattr(rb.sources, name)


def test_factory_builds_a_store_from_a_bucket_object() -> None:
    import rebase as rb

    bucket = _FakeBucket()
    store = rb.sources.energydb(bucket=bucket, prefix="custom")
    assert store.prefix == "custom"
    assert store.bucket is bucket


def test_factory_rejects_a_bucket_without_the_needed_methods() -> None:
    import rebase as rb

    with pytest.raises(Exception, match="must provide a callable"):
        rb.sources.energydb(bucket=object())
