import importlib.util
from datetime import UTC, datetime

import pytest

from rebase.stitch import Exclude, Layer, StitchError, stitch

_HAS_PANDAS = importlib.util.find_spec("pandas") is not None

pandas_only = pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")

ISSUE_TIME = datetime(2021, 6, 10, tzinfo=UTC)


def _series(start: str, values, freq: str = "1h", tz: str | None = "UTC"):
    import pandas as pd

    index = pd.date_range(start=start, periods=len(values), freq=freq, tz=tz)
    return pd.Series(values, index=index)


# --- constructor validation -------------------------------------------------------------


def test_layer_rejects_bad_offsets_and_names() -> None:
    with pytest.raises(ValueError, match="ISO-8601 duration"):
        Layer([], start="soon")
    with pytest.raises(ValueError, match="non-empty string"):
        Layer([], name="  ")


def test_exclude_requires_a_bound() -> None:
    with pytest.raises(ValueError, match="requires start= and/or end="):
        Exclude()


# --- basic stitching --------------------------------------------------------------------


@pandas_only
def test_priority_and_null_fallthrough() -> None:
    import pandas as pd

    top = _series("2021-06-10", [1.0, None, 3.0])
    bottom = _series("2021-06-10", [10.0, 20.0, 30.0])
    result = stitch([Layer(top), Layer(bottom)])
    assert result.tolist() == [1.0, 20.0, 3.0]
    assert isinstance(result.index, pd.DatetimeIndex)
    assert str(result.index.tz) == "UTC"


@pandas_only
def test_windows_split_past_and_future_around_issue_time() -> None:
    forecast = _series("2021-06-09 22:00", [None] * 4 + [1.0, 2.0], freq="1h")
    history = _series("2021-06-09 22:00", [10.0, 20.0, 30.0, 40.0, 50.0, 60.0], freq="1h")
    result = stitch(
        [Layer(forecast, start="PT0H"), Layer(history, end="PT0H")],
        issue_time=ISSUE_TIME,
    )
    # history fills [22:00, 00:00), forecast fills [00:00, ...); forecast nulls at 00:00/01:00 stay null
    assert result.loc["2021-06-09 22:00+00:00"] == 10.0
    assert result.loc["2021-06-09 23:00+00:00"] == 20.0
    assert result.loc["2021-06-10 02:00+00:00"] == 1.0
    assert result.loc["2021-06-10 03:00+00:00"] == 2.0
    import pandas as pd

    assert pd.isna(result.loc["2021-06-10 00:00+00:00"])
    assert pd.isna(result.loc["2021-06-10 01:00+00:00"])


@pandas_only
def test_unbounded_fallback_fills_window_gaps() -> None:
    primary = _series("2021-06-10", [1.0, None, 3.0])
    fallback = _series("2021-06-10", [9.0, 9.0, 9.0])
    result = stitch([Layer(primary, start="PT1H"), Layer(fallback)], issue_time=ISSUE_TIME)
    # primary only applies from 01:00; its 01:00 null falls through to the fallback
    assert result.tolist() == [9.0, 9.0, 3.0]


@pandas_only
def test_union_index_and_absolute_bounds() -> None:
    early = _series("2021-06-10 00:00", [1.0, 2.0])
    late = _series("2021-06-10 05:00", [5.0, 6.0])
    result = stitch([Layer(early, end=datetime(2021, 6, 10, 1, tzinfo=UTC)), Layer(late)])
    # 01:00 is cut by the absolute end bound before the union index is built
    assert len(result) == 3
    assert result.tolist() == [1.0, 5.0, 6.0]
    assert result.index[1] == datetime(2021, 6, 10, 5, tzinfo=UTC)


@pandas_only
def test_negative_offsets() -> None:
    data = _series("2021-06-08", [1.0] * 72, freq="1h")
    result = stitch([Layer(data, start="-P1D", end="PT0H")], issue_time=ISSUE_TIME)
    assert len(result) == 24
    assert result.index[0] == datetime(2021, 6, 9, tzinfo=UTC)
    assert result.index[-1] == datetime(2021, 6, 9, 23, tzinfo=UTC)


# --- Exclude ----------------------------------------------------------------------------


@pandas_only
def test_exclude_blocks_lower_layers_but_not_upper() -> None:
    import pandas as pd

    top = _series("2021-06-10", [1.0, None, None])
    bottom = _series("2021-06-10", [10.0, 20.0, 30.0])
    result, sources = stitch(
        [
            Layer(top, name="fcst"),
            Exclude(start=datetime(2021, 6, 10, 0, tzinfo=UTC), end=datetime(2021, 6, 10, 2, tzinfo=UTC)),
            Layer(bottom, name="climo"),
        ],
        return_sources=True,
    )
    assert result.tolist()[0] == 1.0  # top layer still wins inside the excluded window
    assert pd.isna(result.iloc[1])  # blocked from falling through
    assert result.iloc[2] == 30.0  # outside the window fallback works
    assert sources.tolist() == ["fcst", "excluded", "climo"]


@pandas_only
def test_top_position_exclude_forces_nulls() -> None:
    import pandas as pd

    data = _series("2021-06-10", [1.0, 2.0])
    result = stitch(
        [Exclude(start=datetime(2021, 6, 10, 1, tzinfo=UTC)), Layer(data)],
    )
    assert result.iloc[0] == 1.0
    assert pd.isna(result.iloc[1])


# --- DataFrames -------------------------------------------------------------------------


@pandas_only
def test_dataframe_per_cell_fallback_and_column_union() -> None:
    import pandas as pd

    index = pd.date_range("2021-06-10", periods=2, freq="1h", tz="UTC")
    top = pd.DataFrame({"a": [1.0, None], "b": [None, 4.0]}, index=index)
    bottom = pd.DataFrame({"a": [10.0, 20.0], "c": [7.0, 8.0]}, index=index)
    result, sources = stitch([Layer(top, name="t"), Layer(bottom, name="b")], return_sources=True)
    assert list(result.columns) == ["a", "b", "c"]
    assert result["a"].tolist() == [1.0, 20.0]
    assert pd.isna(result["b"].iloc[0]) and result["b"].iloc[1] == 4.0
    assert result["c"].tolist() == [7.0, 8.0]
    assert sources["a"].tolist() == ["t", "b"]
    assert pd.isna(sources["b"].iloc[0]) and sources["b"].iloc[1] == "t"
    assert sources["c"].tolist() == ["b", "b"]


@pandas_only
def test_dataframe_exclude_applies_to_all_columns() -> None:
    import pandas as pd

    index = pd.date_range("2021-06-10", periods=2, freq="1h", tz="UTC")
    top = pd.DataFrame({"a": [1.0, None]}, index=index)
    bottom = pd.DataFrame({"a": [10.0, 20.0]}, index=index)
    result = stitch(
        [Layer(top), Exclude(start=index[1], end=index[1] + pd.Timedelta("1h")), Layer(bottom)],
    )
    assert result["a"].iloc[0] == 1.0
    assert pd.isna(result["a"].iloc[1])


# --- provenance -------------------------------------------------------------------------


@pandas_only
def test_sources_default_names_and_na() -> None:
    import pandas as pd

    top = _series("2021-06-10", [1.0, None])
    result, sources = stitch([Layer(top)], return_sources=True)
    assert sources.iloc[0] == "layer0"
    assert pd.isna(sources.iloc[1])
    assert sources.dtype == "string"
    assert pd.isna(result.iloc[1])


# --- errors -----------------------------------------------------------------------------


@pandas_only
def test_relative_window_without_issue_time_raises() -> None:
    data = _series("2021-06-10", [1.0])
    with pytest.raises(StitchError, match="relative window but no issue_time"):
        stitch([Layer(data, start="PT0H")])


@pandas_only
def test_naive_data_and_bounds_raise() -> None:
    naive = _series("2021-06-10", [1.0], tz=None)
    with pytest.raises(StitchError, match="naive DatetimeIndex"):
        stitch([Layer(naive)])
    aware = _series("2021-06-10", [1.0])
    with pytest.raises(StitchError, match="naive datetime bound"):
        stitch([Layer(aware, start=datetime(2021, 6, 10))])
    with pytest.raises(ValueError, match="timezone-aware"):
        stitch([Layer(aware, start="PT0H")], issue_time=datetime(2021, 6, 10))


@pandas_only
def test_duplicate_index_raises() -> None:
    import pandas as pd

    index = pd.DatetimeIndex(["2021-06-10", "2021-06-10", "2021-06-10"], tz="UTC")
    data = pd.Series([1.0, 2.0, 3.0], index=index)
    with pytest.raises(StitchError, match="'hist' has 2 duplicate index entries"):
        stitch([Layer(data, name="hist")])


@pandas_only
def test_mixed_kinds_raise() -> None:
    import pandas as pd

    series = _series("2021-06-10", [1.0])
    frame = pd.DataFrame({"a": [1.0]}, index=series.index)
    with pytest.raises(StitchError, match="cannot stitch Series with DataFrame"):
        stitch([Layer(series), Layer(frame)])


def test_empty_and_layerless_lists_raise() -> None:
    with pytest.raises(ValueError, match="at least one Layer"):
        stitch([])
    with pytest.raises(ValueError, match="at least one Layer"):
        stitch([Exclude(start=datetime(2021, 6, 10, tzinfo=UTC))])


@pandas_only
def test_non_pandas_layer_data_raises() -> None:
    with pytest.raises(TypeError, match="pandas Series or DataFrame"):
        stitch([Layer([1, 2, 3])])


# --- index= and empty windows -----------------------------------------------------------


@pandas_only
def test_reindex_onto_target_index() -> None:
    import pandas as pd

    data = _series("2021-06-10", [1.0, 2.0, 3.0])
    target = pd.date_range("2021-06-10 01:00", periods=3, freq="1h", tz="UTC")
    result, sources = stitch([Layer(data)], index=target, return_sources=True)
    assert list(result.index) == list(target)
    assert result.tolist()[:2] == [2.0, 3.0]
    assert pd.isna(result.iloc[2])
    assert pd.isna(sources.iloc[2])


@pandas_only
def test_window_that_eliminates_everything_returns_empty() -> None:
    data = _series("2021-06-10", [1.0, 2.0])
    result = stitch([Layer(data, start="P10D")], issue_time=ISSUE_TIME)
    assert len(result) == 0
