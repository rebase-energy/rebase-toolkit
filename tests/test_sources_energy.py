import importlib.util
from datetime import UTC, datetime

import pytest

from rebase.sources.base import DataSourceError
from rebase.sources.energy import (
    SERIES_VALUES_COLUMNS,
    SeriesKey,
    attach_series_keys,
    build_values_rows,
    select_series_winners,
    series_keys,
    series_values_select,
)

_HAS_PANDAS = importlib.util.find_spec("pandas") is not None
_REPLAY_BOUND = "2026-07-10T09:00:00+00:00"


def test_series_key_id_is_deterministic_and_63bit() -> None:
    a = SeriesKey("portfolio/site-1/t01", "forecast", "electricity.supply")
    b = SeriesKey("portfolio/site-1/t01", "forecast", "electricity.supply")
    c = SeriesKey("portfolio/site-1/t02", "forecast", "electricity.supply")
    d = SeriesKey("portfolio/site-1/t01", "actual", "electricity.supply")
    assert a.series_id == b.series_id
    assert len({a.series_id, c.series_id, d.series_id}) == 3
    assert 0 <= a.series_id < 2**63


def test_series_key_rejects_empty_parts() -> None:
    with pytest.raises(DataSourceError):
        SeriesKey("", "actual", "electricity.demand")


def test_select_latest_collapses_issue_then_correction() -> None:
    key = SeriesKey("p/s", "actual", "electricity.demand")
    sql, params = series_values_select("`ds.series_values`", [key.series_id])
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY series_id, valid_time" in sql
    assert "ORDER BY knowledge_time DESC, change_time DESC" in sql
    assert params == {}


def test_select_overlapping_keeps_every_issue() -> None:
    key = SeriesKey("p/s", "forecast", "electricity.supply")
    sql, _ = series_values_select("`ds.series_values`", [key.series_id], overlapping=True)
    assert "PARTITION BY series_id, valid_time, knowledge_time" in sql
    assert "knowledge_time, value" in sql


def test_select_as_of_bounds_knowledge_time() -> None:
    key = SeriesKey("p/s", "forecast", "electricity.supply")
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    sql, params = series_values_select("`ds.series_values`", [key.series_id], as_of=cutoff)
    assert "knowledge_time <= @as_of" in sql
    assert params["as_of"] == cutoff


def test_select_updates_returns_full_audit_shape() -> None:
    key = SeriesKey("p/s", "actual", "electricity.demand")
    sql, _ = series_values_select("`ds.series_values`", [key.series_id], include_updates=True)
    assert "change_time, value, changed_by, annotation" in sql
    assert "QUALIFY" not in sql


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed")
def test_build_values_rows_simple_shape_stamps_defaults() -> None:
    import pandas as pd

    from rebase.sources.energy import build_values_rows

    key = SeriesKey("p/s", "actual", "electricity.demand")
    df = pd.DataFrame({"value": [1.0, 2.0]}, index=pd.DatetimeIndex(["2026-01-01", "2026-01-02"], name="valid_time"))
    with pytest.warns(UserWarning, match="timezone-naive"):
        rows = build_values_rows(df, key, retention="short", changed_by="etl")

    assert list(rows.columns) == list(SERIES_VALUES_COLUMNS)
    assert (rows["series_id"] == key.series_id).all()
    assert rows["knowledge_time"].notna().all()  # stamped batch-now for SIMPLE
    assert rows["knowledge_time"].nunique() == 1
    assert rows["change_time"].nunique() == 1
    assert (rows["retention"] == "short").all()
    assert (rows["changed_by"] == "etl").all()
    assert str(rows["valid_time"].dtype) == "datetime64[us, UTC]"


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed")
def test_build_values_rows_versioned_multiindex_contract() -> None:
    import pandas as pd

    from rebase.sources.energy import build_values_rows

    key = SeriesKey("p/s", "forecast", "electricity.supply")
    # timedatamodel VERSIONED to_pandas contract: (knowledge_time, valid_time) MultiIndex
    idx = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2026-01-01T06:00Z"), pd.Timestamp("2026-01-02T00:00Z")),
            (pd.Timestamp("2026-01-01T18:00Z"), pd.Timestamp("2026-01-02T00:00Z")),
        ],
        names=["knowledge_time", "valid_time"],
    )
    rows = build_values_rows(pd.DataFrame({"value": [10.0, 11.0]}, index=idx), key)
    assert rows["knowledge_time"].nunique() == 2  # user-supplied issues preserved
    assert rows["valid_time"].nunique() == 1


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed")
def test_build_values_rows_rejects_audit_shape() -> None:
    import pandas as pd

    from rebase.sources.energy import build_values_rows

    key = SeriesKey("p/s", "actual", "electricity.demand")
    df = pd.DataFrame(
        {
            "valid_time": [pd.Timestamp("2026-01-01T00:00Z")],
            "change_time": [pd.Timestamp("2026-01-01T00:00Z")],
            "value": [1.0],
        }
    )
    with pytest.raises(DataSourceError, match="read-only"):
        build_values_rows(df, key)


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_build_values_rows_stamps_the_replay_bound(monkeypatch) -> None:
    import pandas as pd

    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", _REPLAY_BOUND)
    frame = pd.DataFrame({"valid_time": pd.to_datetime(["2026-07-10T00:00Z"]), "value": [1.0]})
    rows = build_values_rows(frame, SeriesKey("p", "actual", "electricity.load"))
    assert rows["knowledge_time"].iloc[0] == pd.Timestamp(_REPLAY_BOUND)
    assert rows["change_time"].iloc[0] == pd.Timestamp(_REPLAY_BOUND)


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_build_values_rows_honours_a_passed_knowledge_time() -> None:
    import pandas as pd

    frame = pd.DataFrame({"valid_time": pd.to_datetime(["2026-07-10T00:00Z"]), "value": [1.0]})
    moment = datetime(2026, 7, 9, 18, 0, tzinfo=UTC)
    rows = build_values_rows(frame, SeriesKey("p", "actual", "electricity.load"), knowledge_time=moment)
    assert rows["knowledge_time"].iloc[0] == pd.Timestamp(moment)


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_frame_knowledge_time_wins_over_the_argument() -> None:
    import pandas as pd

    frame = pd.DataFrame(
        {
            "valid_time": pd.to_datetime(["2026-07-10T00:00Z"]),
            "knowledge_time": pd.to_datetime(["2026-07-09T12:00Z"]),
            "value": [1.0],
        }
    )
    rows = build_values_rows(
        frame,
        SeriesKey("p", "actual", "electricity.load"),
        knowledge_time=datetime(2026, 7, 9, 18, 0, tzinfo=UTC),
    )
    assert rows["knowledge_time"].iloc[0] == pd.Timestamp("2026-07-09T12:00Z")


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed")
def test_read_series_maps_ids_back_to_keys(monkeypatch) -> None:
    import pandas as pd

    from rebase.sources.bigquery import BigQuerySource

    key = SeriesKey("p/s", "actual", "electricity.demand")
    source = BigQuerySource(project="proj")

    def fake_read(sql, params=None):
        assert str(key.series_id) in sql
        return pd.DataFrame(
            {
                "series_id": [key.series_id],
                "valid_time": [pd.Timestamp("2026-01-01T00:00Z")],
                "value": [1.0],
            }
        )

    monkeypatch.setattr(source, "read", fake_read)
    out = source.read_series("ds", ("p/s", "actual", "electricity.demand"))
    assert list(out.columns) == ["path", "data_type", "name", "valid_time", "value"]
    assert out.loc[0, "name"] == "electricity.demand"
    assert "series_id" not in out.columns  # energydb convention: ids never exposed


# --- winner selection ------------------------------------------------------------------

_SV_COLUMNS = list(SERIES_VALUES_COLUMNS)


def _values_frame(rows):
    """Build a raw series_values frame from (valid_time, knowledge_time, change_time, value) tuples."""
    import pandas as pd

    records = []
    for valid_time, knowledge_time, change_time, value in rows:
        records.append(
            {
                "series_id": 7,
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
        )
    return pd.DataFrame.from_records(records, columns=_SV_COLUMNS)


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_winners_pick_the_latest_knowledge_then_change_time() -> None:
    frame = _values_frame(
        [
            ("2026-01-01T00:00Z", "2026-01-01T06:00Z", "2026-01-01T06:00Z", 1.0),
            ("2026-01-01T00:00Z", "2026-01-01T09:00Z", "2026-01-01T09:00Z", 2.0),
            ("2026-01-01T00:00Z", "2026-01-01T09:00Z", "2026-01-01T10:00Z", 3.0),
        ]
    )
    out = select_series_winners(frame)
    assert list(out.columns) == ["series_id", "valid_time", "value"]
    assert list(out["value"]) == [3.0]


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_winners_respect_as_of() -> None:
    import pandas as pd

    frame = _values_frame(
        [
            ("2026-01-01T00:00Z", "2026-01-01T06:00Z", "2026-01-01T06:00Z", 1.0),
            ("2026-01-01T00:00Z", "2026-01-01T09:00Z", "2026-01-01T09:00Z", 2.0),
        ]
    )
    out = select_series_winners(frame, as_of=pd.Timestamp("2026-01-01T07:00Z").to_pydatetime())
    assert list(out["value"]) == [1.0]


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_winners_overlapping_keeps_every_issue() -> None:
    frame = _values_frame(
        [
            ("2026-01-01T00:00Z", "2026-01-01T06:00Z", "2026-01-01T06:00Z", 1.0),
            ("2026-01-01T00:00Z", "2026-01-01T09:00Z", "2026-01-01T09:00Z", 2.0),
            ("2026-01-01T00:00Z", "2026-01-01T09:00Z", "2026-01-01T10:00Z", 3.0),
        ]
    )
    out = select_series_winners(frame, overlapping=True)
    assert list(out.columns) == ["series_id", "valid_time", "knowledge_time", "value"]
    assert sorted(out["value"]) == [1.0, 3.0]


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_winners_include_updates_returns_the_audit_shape() -> None:
    frame = _values_frame(
        [
            ("2026-01-01T00:00Z", "2026-01-01T09:00Z", "2026-01-01T10:00Z", 3.0),
            ("2026-01-01T00:00Z", "2026-01-01T06:00Z", "2026-01-01T06:00Z", 1.0),
        ]
    )
    out = select_series_winners(frame, include_updates=True)
    assert list(out.columns) == [
        "series_id",
        "valid_time",
        "knowledge_time",
        "change_time",
        "value",
        "changed_by",
        "annotation",
    ]
    assert list(out["value"]) == [1.0, 3.0]


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_winners_on_an_empty_frame_returns_empty_with_the_right_columns() -> None:
    out = select_series_winners(_values_frame([]))
    assert list(out.columns) == ["series_id", "valid_time", "value"]
    assert len(out) == 0


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_winners_default_as_of_to_the_replay_bound(monkeypatch) -> None:
    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", "2026-01-01T07:00:00+00:00")
    frame = _values_frame(
        [
            ("2026-01-01T00:00Z", "2026-01-01T06:00Z", "2026-01-01T06:00Z", 1.0),
            ("2026-01-01T00:00Z", "2026-01-01T09:00Z", "2026-01-01T09:00Z", 2.0),
        ]
    )
    assert list(select_series_winners(frame)["value"]) == [1.0]


# --- series key helpers ---------------------------------------------------------------


def test_series_keys_accepts_keys_tuples_and_lists() -> None:
    key = SeriesKey("p", "actual", "electricity.load")
    assert series_keys(key) == [key]
    assert series_keys(("p", "actual", "electricity.load")) == [key]
    assert series_keys([key, ("q", "forecast", "electricity.supply")])[1].path == "q"


@pytest.mark.parametrize("bad", [42, "p", ("p", "actual"), [("p", "actual")]])
def test_series_keys_rejects_bad_input(bad) -> None:
    with pytest.raises(DataSourceError, match="series keys must be SeriesKey"):
        series_keys(bad)


def test_series_keys_rejects_an_empty_list() -> None:
    with pytest.raises(DataSourceError, match="at least one series key"):
        series_keys([])


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_attach_series_keys_replaces_the_id_column() -> None:
    import pandas as pd

    key = SeriesKey("p", "actual", "electricity.load")
    frame = pd.DataFrame(
        {"series_id": [key.series_id], "valid_time": [pd.Timestamp("2026-01-01T00:00Z")], "value": [1.0]}
    )
    out = attach_series_keys(frame, {key.series_id: key})
    assert list(out.columns) == ["path", "data_type", "name", "valid_time", "value"]
    assert out["path"].iloc[0] == "p"
    assert "series_id" in frame.columns  # the caller's frame is untouched
