import importlib.util
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from rebase.timing import Duration, ForecastWindow, parse_offset

_HAS_PANDAS = importlib.util.find_spec("pandas") is not None

pandas_only = pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")


# --- Duration.parse ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("PT1H", Duration(seconds=3600)),
        ("PT30M", Duration(seconds=1800)),
        ("PT7.5S", Duration(seconds=7.5)),
        ("P10D", Duration(days=10)),
        ("P2W", Duration(days=14)),
        ("P3M", Duration(months=3)),
        ("P2Y", Duration(months=24)),
        ("P1Y2M3W4DT5H6M7.5S", Duration(months=14, days=25, seconds=5 * 3600 + 6 * 60 + 7.5)),
        ("-P2D", Duration(days=-2)),
        ("-PT1H", Duration(seconds=-3600)),
        ("PT0S", Duration()),
    ],
)
def test_parse_iso_durations(text, expected) -> None:
    assert Duration.parse(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("45m", Duration(seconds=45 * 60)),
        ("2h", Duration(seconds=2 * 3600)),
        ("900s", Duration(seconds=900)),
        ("-1d", Duration(seconds=-86400)),
    ],
)
def test_parse_compact_durations(text, expected) -> None:
    assert Duration.parse(text) == expected


@pytest.mark.parametrize("text", ["", "P", "PT", "-P", "P1.5D", "P1S", "1x", "2H", "1M ", "PT1H30", "x"])
def test_parse_rejects_bad_grammar(text) -> None:
    with pytest.raises(ValueError, match="ISO-8601 duration"):
        Duration.parse(text)


def test_parse_rejects_non_string() -> None:
    not_a_string: Any = 3600
    with pytest.raises(TypeError, match="must be a string"):
        Duration.parse(not_a_string)


def test_coerce_accepts_timedelta_duration_and_string() -> None:
    assert Duration.coerce(timedelta(hours=2)) == Duration(seconds=7200)
    assert Duration.coerce(Duration(days=1)) == Duration(days=1)
    assert Duration.coerce("P1D") == Duration(days=1)
    for bad in (True, 1):
        bad_value: Any = bad
        with pytest.raises(TypeError, match="timedelta or a Duration"):
            Duration.coerce(bad_value)


def test_parse_offset_passes_none_through() -> None:
    assert parse_offset(None) is None
    assert parse_offset("PT1H") == Duration(seconds=3600)


# --- calendar arithmetic ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "duration", "expected"),
    [
        (datetime(2021, 1, 31, tzinfo=UTC), "P1M", datetime(2021, 2, 28, tzinfo=UTC)),
        (datetime(2020, 1, 31, tzinfo=UTC), "P1M", datetime(2020, 2, 29, tzinfo=UTC)),
        (datetime(2020, 2, 29, tzinfo=UTC), "P1Y", datetime(2021, 2, 28, tzinfo=UTC)),
        (datetime(2021, 3, 31, tzinfo=UTC), "-P1M", datetime(2021, 2, 28, tzinfo=UTC)),
        (datetime(2021, 1, 1, tzinfo=UTC), "P2W", datetime(2021, 1, 15, tzinfo=UTC)),
        (datetime(2021, 1, 1, tzinfo=UTC), "-P2D", datetime(2020, 12, 30, tzinfo=UTC)),
        (datetime(2021, 1, 1, tzinfo=UTC), "PT36H", datetime(2021, 1, 2, 12, tzinfo=UTC)),
        (datetime(2021, 11, 30, tzinfo=UTC), "P3M", datetime(2022, 2, 28, tzinfo=UTC)),
    ],
)
def test_add_to_is_calendar_correct(start, duration, expected) -> None:
    assert Duration.parse(duration).add_to(start) == expected


@pytest.mark.parametrize(
    ("text", "canonical"),
    [
        ("PT1H", "PT1H"),
        ("P10D", "P10D"),
        ("P2W", "P14D"),
        ("P2Y", "P2Y"),
        ("P14M", "P1Y2M"),
        ("-P2D", "-P2D"),
        ("PT90M", "PT1H30M"),
        ("PT7.5S", "PT7.5S"),
        ("45m", "PT45M"),
        ("2h", "PT2H"),
        ("PT0S", "PT0S"),
    ],
)
def test_isoformat_round_trip(text, canonical) -> None:
    duration = Duration.parse(text)
    assert duration.isoformat() == canonical
    assert Duration.parse(canonical) == duration


# --- ForecastWindow ---------------------------------------------------------------------


def test_window_requires_end() -> None:
    with pytest.raises(ValueError, match="requires end="):
        ForecastWindow(start="PT1H")


def test_window_default_start_is_issue_time() -> None:
    window = ForecastWindow(end="P1D")
    start, end = window.resolve(datetime(2021, 1, 1, tzinfo=UTC))
    assert start == datetime(2021, 1, 1, tzinfo=UTC)
    assert end == datetime(2021, 1, 2, tzinfo=UTC)


def test_window_rejects_start_after_end() -> None:
    with pytest.raises(ValueError, match="must be before end"):
        ForecastWindow(start="P2D", end="P1D")
    with pytest.raises(ValueError, match="must be before end"):
        ForecastWindow(start="PT1H", end="PT1H")


def test_window_dict_round_trip() -> None:
    window = ForecastWindow(start="PT1H", end="P10D")
    payload = window.to_dict()
    assert payload == {"type": "forecast_window", "start": "PT1H", "end": "P10D"}
    assert ForecastWindow.from_dict(payload) == window
    assert ForecastWindow.coerce(payload) == window
    assert ForecastWindow.coerce(window) is window


def test_window_canonicalises_compact_grammar() -> None:
    window = ForecastWindow(start="1h", end="2d")
    assert window.to_dict() == {"type": "forecast_window", "start": "PT1H", "end": "PT48H"}


def test_window_from_dict_rejects_bad_payloads() -> None:
    with pytest.raises(ValueError, match='type="forecast_window"'):
        ForecastWindow.from_dict({"type": "cron", "start": "PT1H", "end": "P1D"})
    not_a_dict: Any = "PT1H"
    with pytest.raises(TypeError, match="expects a dict"):
        ForecastWindow.from_dict(not_a_dict)
    not_a_window: Any = 42
    with pytest.raises(TypeError, match="ForecastWindow or its dict form"):
        ForecastWindow.coerce(not_a_window)


def test_window_resolve_accepts_iso_string_like_ctx_fired_at() -> None:
    window = ForecastWindow(start="PT1H", end="PT49H")
    start, end = window.resolve("2021-06-01T09:00:00+00:00")
    assert start == datetime(2021, 6, 1, 10, tzinfo=UTC)
    assert end == datetime(2021, 6, 3, 10, tzinfo=UTC)
    start_z, end_z = window.resolve("2021-06-01T09:00:00Z")
    assert (start_z, end_z) == (start, end)


def test_window_resolve_rejects_naive_issue_time() -> None:
    window = ForecastWindow(end="P1D")
    with pytest.raises(ValueError, match="timezone-aware"):
        window.resolve(datetime(2021, 1, 1))
    with pytest.raises(ValueError, match="ISO-8601 timestamp"):
        window.resolve("yesterday")


def test_window_negative_start_reaches_into_the_past() -> None:
    window = ForecastWindow(start="-P2D", end="PT0S")
    start, end = window.resolve(datetime(2021, 1, 10, tzinfo=UTC))
    assert start == datetime(2021, 1, 8, tzinfo=UTC)
    assert end == datetime(2021, 1, 10, tzinfo=UTC)


def test_workflow_default_is_stored_as_dict() -> None:
    import rebase as rb

    @rb.workflow(name="forecast-window-default")
    def forecast(ctx=None, window=rb.ForecastWindow(start="PT1H", end="P10D")) -> dict:
        return {}

    assert forecast.default_parameters == {"window": {"type": "forecast_window", "start": "PT1H", "end": "P10D"}}
    round_tripped = ForecastWindow.coerce(forecast.default_parameters["window"])
    assert round_tripped == rb.ForecastWindow(start="PT1H", end="P10D")


@pandas_only
def test_window_range_is_end_exclusive() -> None:
    window = ForecastWindow(start="PT0S", end="P1D")
    index = window.range(datetime(2021, 1, 1, tzinfo=UTC), freq="1h")
    assert len(index) == 24
    assert index[0] == datetime(2021, 1, 1, tzinfo=UTC)
    assert index[-1] == datetime(2021, 1, 1, 23, tzinfo=UTC)
