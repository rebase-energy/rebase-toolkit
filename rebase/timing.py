"""Durations and forecast windows.

:class:`Duration` parses ISO-8601 duration strings ("PT1H", "P10D", "-P2D") as well
as the toolkit's compact grammar ("45m", "2h") and applies them with calendar-correct
arithmetic. :class:`ForecastWindow` describes a forecast run's target range as a pair
of offsets relative to an issue time — the standard vocabulary for "every hour,
forecast 1 hour through 10 days ahead" — and serialises to a plain JSON dict so it
can be used as a workflow parameter default and resolved inside the run from
``ctx.fired_at``.

pandas is imported lazily inside the few methods that need it so this module stays
importable with the toolkit's core (pandas-free) dependencies.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

_ISO_DURATION_PATTERN = re.compile(
    r"^(?P<sign>-)?P"
    r"(?:(?P<years>\d+)Y)?"
    r"(?:(?P<months>\d+)M)?"
    r"(?:(?P<weeks>\d+)W)?"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T"
    r"(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?"
    r")?$"
)
_COMPACT_DURATION_PATTERN = re.compile(r"^(?P<sign>-)?(?P<value>\d+)\s*(?P<unit>[smhd])$")
_COMPACT_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

_DURATION_HINT = "an ISO-8601 duration like 'PT1H' or 'P10D' (or compact '45m')"


def _grammar_error(field_name: str, value: Any) -> ValueError:
    return ValueError(f"{field_name} must be {_DURATION_HINT}; got {value!r}")


class Duration:
    """A signed duration split into calendar months, days and seconds.

    Months are kept separate because "P1M" must respect calendar month lengths;
    ``add_to`` clamps the day of month (Jan 31 + P1M -> Feb 28/29).
    """

    __slots__ = ("months", "days", "seconds")

    def __init__(self, *, months: int = 0, days: int = 0, seconds: float = 0.0) -> None:
        for name, value in (("months", months), ("days", days)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"Duration {name} must be an int; got {value!r}")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise TypeError(f"Duration seconds must be a number; got {seconds!r}")
        self.months = months
        self.days = days
        self.seconds = float(seconds)

    @classmethod
    def parse(cls, text: str, *, field_name: str = "duration") -> Duration:
        if not isinstance(text, str):
            raise TypeError(f"{field_name} must be a string; got {type(text).__name__}")
        stripped = text.strip()
        match = _ISO_DURATION_PATTERN.match(stripped)
        if match is not None:
            parts = match.groupdict()
            if all(parts[key] is None for key in ("years", "months", "weeks", "days", "hours", "minutes", "seconds")):
                raise _grammar_error(field_name, text)
            sign = -1 if parts["sign"] else 1
            months = int(parts["years"] or 0) * 12 + int(parts["months"] or 0)
            days = int(parts["weeks"] or 0) * 7 + int(parts["days"] or 0)
            seconds = (
                float(parts["hours"] or 0) * 3600 + float(parts["minutes"] or 0) * 60 + float(parts["seconds"] or 0)
            )
            return cls(months=sign * months, days=sign * days, seconds=sign * seconds)
        match = _COMPACT_DURATION_PATTERN.match(stripped)
        if match is not None:
            sign = -1 if match.group("sign") else 1
            seconds = int(match.group("value")) * _COMPACT_UNIT_SECONDS[match.group("unit")]
            return cls(seconds=sign * seconds)
        raise _grammar_error(field_name, text)

    @classmethod
    def coerce(cls, value: str | timedelta | Duration, *, field_name: str = "duration") -> Duration:
        if isinstance(value, Duration):
            return value
        if isinstance(value, timedelta):
            return cls(seconds=value.total_seconds())
        if isinstance(value, str):
            return cls.parse(value, field_name=field_name)
        raise TypeError(f"{field_name} must be {_DURATION_HINT}, a timedelta or a Duration; got {type(value).__name__}")

    def add_to(self, moment: datetime) -> datetime:
        """Add this duration to ``moment`` with calendar-correct month arithmetic."""
        result = moment
        if self.months:
            total = moment.month - 1 + self.months
            year = moment.year + total // 12
            month = total % 12 + 1
            day = min(moment.day, _days_in_month(year, month))
            result = moment.replace(year=year, month=month, day=day)
        return result + timedelta(days=self.days, seconds=self.seconds)

    def isoformat(self) -> str:
        """Canonical ISO-8601 form, e.g. ``-P1M2DT3H``."""
        if self.months == 0 and self.days == 0 and self.seconds == 0:
            return "PT0S"
        negative = self.months < 0 or self.days < 0 or self.seconds < 0
        positive = self.months > 0 or self.days > 0 or self.seconds > 0
        if negative and positive:
            raise ValueError("cannot render a mixed-sign Duration as ISO-8601")
        months, days, seconds = abs(self.months), abs(self.days), abs(self.seconds)
        date_part = ""
        if months >= 12:
            date_part += f"{months // 12}Y"
            months %= 12
        if months:
            date_part += f"{months}M"
        if days:
            date_part += f"{days}D"
        time_part = ""
        hours = int(seconds // 3600)
        seconds -= hours * 3600
        minutes = int(seconds // 60)
        seconds -= minutes * 60
        if hours:
            time_part += f"{hours}H"
        if minutes:
            time_part += f"{minutes}M"
        if seconds:
            rendered = f"{seconds:g}"
            time_part += f"{rendered}S"
        return ("-" if negative else "") + "P" + date_part + (f"T{time_part}" if time_part else "")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Duration):
            return NotImplemented
        return (self.months, self.days, self.seconds) == (other.months, other.days, other.seconds)

    def __hash__(self) -> int:
        return hash((self.months, self.days, self.seconds))

    def __repr__(self) -> str:
        return (
            f"Duration({self.isoformat()!r})"
            if self._single_signed()
            else (f"Duration(months={self.months}, days={self.days}, seconds={self.seconds})")
        )

    def _single_signed(self) -> bool:
        negative = self.months < 0 or self.days < 0 or self.seconds < 0
        positive = self.months > 0 or self.days > 0 or self.seconds > 0
        return not (negative and positive)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (datetime(year, month + 1, 1) - datetime(year, month, 1)).days


def parse_offset(value: str | timedelta | Duration | None, *, field_name: str = "offset") -> Duration | None:
    """Coerce an optional relative offset; ``None`` passes through."""
    if value is None:
        return None
    return Duration.coerce(value, field_name=field_name)


def _coerce_issue_time(issue_time: datetime | str, *, field_name: str = "issue_time") -> datetime:
    if isinstance(issue_time, str):
        try:
            issue_time = datetime.fromisoformat(issue_time.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"{field_name} must be an ISO-8601 timestamp; got {issue_time!r}") from None
    if not isinstance(issue_time, datetime):
        raise TypeError(f"{field_name} must be a datetime or ISO-8601 string; got {type(issue_time).__name__}")
    if issue_time.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware; use datetime.now(UTC)")
    return issue_time


FORECAST_WINDOW_TYPE = "forecast_window"


class ForecastWindow:
    """A forecast target range as offsets relative to an issue time.

    ``ForecastWindow(start="PT1H", end="P10D")`` means "from 1 hour after the issue
    time up to (but excluding) 10 days after it". Serialises to a plain dict so it
    survives the JSON round-trip through workflow parameters; inside a workflow use
    ``window = rebase.ForecastWindow.coerce(window)`` and resolve against
    ``ctx.fired_at``.
    """

    def __init__(
        self, start: str | timedelta | Duration = "PT0H", end: str | timedelta | Duration | None = None
    ) -> None:
        if end is None:
            raise ValueError('ForecastWindow requires end=, e.g. ForecastWindow(start="PT1H", end="P10D")')
        self._start_duration = Duration.coerce(start, field_name="start")
        self._end_duration = Duration.coerce(end, field_name="end")
        self.start = self._start_duration.isoformat()
        self.end = self._end_duration.isoformat()
        if self._start_duration.months == self._end_duration.months:
            anchor = datetime(2000, 1, 1)
            if self._start_duration.add_to(anchor) >= self._end_duration.add_to(anchor):
                raise ValueError(f"ForecastWindow start ({self.start}) must be before end ({self.end})")

    def to_dict(self) -> dict[str, Any]:
        return {"type": FORECAST_WINDOW_TYPE, "start": self.start, "end": self.end}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ForecastWindow:
        if not isinstance(data, dict):
            raise TypeError(f"ForecastWindow.from_dict expects a dict; got {type(data).__name__}")
        kind = data.get("type")
        if kind != FORECAST_WINDOW_TYPE:
            raise ValueError(f'ForecastWindow dict must have type="{FORECAST_WINDOW_TYPE}"; got {kind!r}')
        return cls(start=data.get("start", "PT0H"), end=data.get("end"))

    @classmethod
    def coerce(cls, value: ForecastWindow | dict[str, Any]) -> ForecastWindow:
        if isinstance(value, ForecastWindow):
            return value
        if isinstance(value, dict):
            return cls.from_dict(value)
        raise TypeError(f"expected a ForecastWindow or its dict form; got {type(value).__name__}")

    def resolve(self, issue_time: datetime | str) -> tuple[datetime, datetime]:
        """Concrete ``(start, end)`` datetimes for an issue time; end-exclusive."""
        moment = _coerce_issue_time(issue_time)
        start = self._start_duration.add_to(moment)
        end = self._end_duration.add_to(moment)
        if start >= end:
            raise ValueError(
                f"ForecastWindow start ({self.start}) must resolve before end ({self.end}) at {moment.isoformat()}"
            )
        return start, end

    def range(self, issue_time: datetime | str, freq: str) -> Any:
        """A pandas DatetimeIndex over the resolved window (end-exclusive)."""
        try:
            import pandas as pd
        except ImportError:  # pragma: no cover - exercised only without pandas
            raise ImportError("ForecastWindow.range() requires pandas; install it with: pip install pandas") from None
        start, end = self.resolve(issue_time)
        return pd.date_range(start=start, end=end, freq=freq, inclusive="left")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ForecastWindow):
            return NotImplemented
        return (self.start, self.end) == (other.start, other.end)

    def __repr__(self) -> str:
        return f"ForecastWindow(start={self.start!r}, end={self.end!r})"
