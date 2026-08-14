"""Dataset contracts and freshness configuration.

A :class:`Contract` declares the columns, ranges and table-level policies a dataset's
frames must satisfy; a :class:`Freshness` declares how recently it must have been
signalled. Both serialise to plain dicts (JSON Schema plus an ``x-rebase`` block for
the contract) that are stored on the dataset via ``PATCH /datasets/{name}``.

The validation engine (:func:`compile_checks` / :func:`validate_frame`) runs the
contract against a pandas DataFrame. pandas is imported lazily inside functions so
this module stays importable with the toolkit's core (pandas-free) dependencies.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime as _datetime
from datetime import timedelta
from typing import Any

# client.py never imports this module at module level (Dataset takes contracts as
# duck-typed ``to_dict`` objects or plain dicts), so this import cannot cycle.
from rebase.client import RebaseWorkflowError


class ContractViolation(RebaseWorkflowError):
    """Raised when a frame fails its dataset contract under ``on_violation="fail"``."""

    def __init__(self, message: str, *, report: ValidationReport | None = None) -> None:
        super().__init__(message)
        self.report = report


CONTRACT_SCHEMA = "rebase/contract-v1"

_DTYPES = ("timestamp", "date", "float", "int", "string", "bool")
_BETWEEN_DTYPES = {"timestamp", "date", "float", "int"}
_ISIN_DTYPES = {"string", "int"}
_GAP_DTYPES = {"timestamp", "date"}
_MONOTONIC_DTYPES = {"timestamp", "date", "float", "int"}
_DTYPE_TO_PROPERTY: dict[str, dict[str, str]] = {
    "timestamp": {"type": "string", "format": "date-time"},
    "date": {"type": "string", "format": "date"},
    "float": {"type": "number"},
    "int": {"type": "integer"},
    "string": {"type": "string"},
    "bool": {"type": "boolean"},
}

MAX_REPORT_FAILURES = 20
MAX_SAMPLE_ROWS = 10


def _json_bound(value: Any) -> Any:
    """Serialise a between-bound to something JSON-storable (dates become ISO strings)."""
    if isinstance(value, (_datetime, _date)):
        return value.isoformat()
    if hasattr(value, "isoformat"):  # pd.Timestamp and friends
        return value.isoformat()
    return value


class Column:
    """One declared dataset column with optional value constraints."""

    def __init__(
        self,
        name: str,
        dtype: str,
        *,
        not_null: bool = False,
        between: tuple[Any, Any] | list[Any] | None = None,
        isin: list[Any] | None = None,
        max_null_run: int | None = None,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Column requires a non-empty name")
        if dtype not in _DTYPES:
            raise ValueError(f"Column dtype must be one of {', '.join(_DTYPES)}; got {dtype!r}")
        if between is not None and isin is not None:
            raise ValueError(f"Column {name!r}: set at most one of between= or isin=, not both")
        if between is not None:
            if dtype not in _BETWEEN_DTYPES:
                raise ValueError(f"Column {name!r}: between= only applies to {sorted(_BETWEEN_DTYPES)} columns")
            if not isinstance(between, (tuple, list)) or len(between) != 2:
                raise ValueError(f"Column {name!r}: between= must be a (low, high) 2-tuple")
            if between[0] is None and between[1] is None:
                raise ValueError(f"Column {name!r}: between= must set at least one bound")
            between = (between[0], between[1])
        if isin is not None:
            if dtype not in _ISIN_DTYPES:
                raise ValueError(f"Column {name!r}: isin= only applies to {sorted(_ISIN_DTYPES)} columns")
            if not isinstance(isin, (tuple, list)) or not len(isin):
                raise ValueError(f"Column {name!r}: isin= must be a non-empty list")
            isin = list(isin)
        if max_null_run is not None and (
            isinstance(max_null_run, bool) or not isinstance(max_null_run, int) or max_null_run < 1
        ):
            raise ValueError(f"Column {name!r}: max_null_run must be a positive integer or None")
        self.name = name.strip()
        self.dtype = dtype
        self.not_null = bool(not_null)
        self.between = between
        self.isin = isin
        self.max_null_run = max_null_run

    def to_property(self) -> dict[str, Any]:
        prop: dict[str, Any] = dict(_DTYPE_TO_PROPERTY[self.dtype])
        if self.between is not None:
            low, high = self.between
            if low is not None:
                prop["minimum"] = _json_bound(low)
            if high is not None:
                prop["maximum"] = _json_bound(high)
        if self.isin is not None:
            prop["enum"] = list(self.isin)
        if self.not_null:
            prop["x-not-null"] = True
        if self.max_null_run is not None:
            prop["x-max-null-run"] = self.max_null_run
        return prop

    @classmethod
    def from_property(cls, name: str, prop: dict[str, Any], *, required: bool = False) -> Column:
        """Rebuild a Column from a stored JSON Schema property (unknown keys ignored)."""
        json_type = prop.get("type")
        json_format = prop.get("format")
        if json_type == "string" and json_format == "date-time":
            dtype = "timestamp"
        elif json_type == "string" and json_format == "date":
            dtype = "date"
        elif json_type == "number":
            dtype = "float"
        elif json_type == "integer":
            dtype = "int"
        elif json_type == "boolean":
            dtype = "bool"
        elif json_type == "string":
            dtype = "string"
        else:
            raise ValueError(f"Column {name!r}: unsupported stored property type {json_type!r}")
        between = None
        if prop.get("minimum") is not None or prop.get("maximum") is not None:
            between = (prop.get("minimum"), prop.get("maximum"))
        isin = list(prop["enum"]) if prop.get("enum") else None
        return cls(
            name,
            dtype,
            not_null=bool(required or prop.get("x-not-null")),
            between=between,
            isin=isin,
            max_null_run=prop.get("x-max-null-run"),
        )


class Index:
    """Row-order and spacing constraints on a dataset's index column.

    ``monotonic=True`` means *strictly increasing*: duplicate index values violate it.
    ``max_gap`` bounds the distance between consecutive rows **as delivered** — the frame is
    never sorted first, because monotonicity is a separate assertion and sorting would
    quietly repair a frame that failed it.
    """

    def __init__(
        self,
        column: str,
        *,
        monotonic: bool = False,
        max_gap: str | int | float | timedelta | Any | None = None,
    ) -> None:
        from rebase.timing import Duration

        if not isinstance(column, str) or not column.strip():
            raise ValueError("Index requires a non-empty column name")
        if not monotonic and max_gap is None:
            raise ValueError(f"Index {column!r} must declare monotonic=True, max_gap=..., or both")
        gap: str | None = None
        if max_gap is not None:
            if isinstance(max_gap, bool):
                raise TypeError("Index max_gap must be a duration string, seconds, timedelta or Duration")
            if isinstance(max_gap, (int, float)):
                max_gap = timedelta(seconds=float(max_gap))
            duration = Duration.coerce(max_gap, field_name="Index max_gap")
            if duration.months:
                raise ValueError(
                    f"Index {column!r}: max_gap cannot be a calendar duration; "
                    "a month has no fixed length and the check compares a fixed timedelta"
                )
            if duration.days <= 0 and duration.seconds <= 0:
                raise ValueError(f"Index {column!r}: max_gap must be positive")
            gap = duration.isoformat()
        self.column = column.strip()
        self.monotonic = bool(monotonic)
        self.max_gap = gap

    def gap_timedelta(self) -> timedelta | None:
        """``max_gap`` as a fixed timedelta, or ``None`` when unset."""
        if self.max_gap is None:
            return None
        from rebase.timing import Duration

        duration = Duration.parse(self.max_gap, field_name="Index max_gap")
        return timedelta(days=duration.days, seconds=duration.seconds)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"column": self.column}
        if self.monotonic:
            payload["monotonic"] = True
        if self.max_gap is not None:
            payload["max_gap"] = self.max_gap
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Index:
        """Rebuild an Index from its stored form (unknown keys ignored)."""
        if not isinstance(data, dict):
            raise TypeError("Index.from_dict expects a dict")
        return cls(
            data.get("column", ""),
            monotonic=bool(data.get("monotonic", False)),
            max_gap=data.get("max_gap"),
        )


class Contract:
    """A dataset contract: declared columns plus table-level policies."""

    def __init__(
        self,
        columns: list[Column],
        *,
        primary_key: tuple[str, ...] | list[str] = (),
        min_rows: int | None = None,
        extra: str = "ignore",
        on_violation: str = "fail",
        watermark_column: str | None = None,
        index: Index | None = None,
        require_contract: bool = False,
    ) -> None:
        if not isinstance(columns, (list, tuple)) or not columns:
            raise ValueError("Contract requires a non-empty list of Column")
        for column in columns:
            if not isinstance(column, Column):
                raise TypeError("Contract columns must be rebase.Column instances")
        names = [column.name for column in columns]
        if len(set(names)) != len(names):
            raise ValueError("Contract column names must be unique")
        known = set(names)
        primary_key = tuple(primary_key or ())
        missing_pk = [key for key in primary_key if key not in known]
        if missing_pk:
            raise ValueError(f"Contract primary_key references undeclared column(s) {missing_pk}")
        if watermark_column is not None and watermark_column not in known:
            raise ValueError(f"Contract watermark_column {watermark_column!r} is not a declared column")
        if index is not None:
            if not isinstance(index, Index):
                raise TypeError("Contract index must be a rebase.Index instance")
            if index.column not in known:
                raise ValueError(f"Contract index column {index.column!r} is not a declared column")
            index_dtype = next(column.dtype for column in columns if column.name == index.column)
            if index.max_gap is not None and index_dtype not in _GAP_DTYPES:
                raise ValueError(
                    f"Contract index {index.column!r}: max_gap requires a {' or '.join(sorted(_GAP_DTYPES))} "
                    f"column; got {index_dtype}"
                )
            if index.monotonic and index_dtype not in _MONOTONIC_DTYPES:
                raise ValueError(
                    f"Contract index {index.column!r}: monotonic requires one of "
                    f"{sorted(_MONOTONIC_DTYPES)}; got {index_dtype}"
                )
        null_run_columns = [column.name for column in columns if column.max_null_run is not None]
        if null_run_columns and index is None:
            raise ValueError(
                f"Contract column(s) {null_run_columns} declare max_null_run; max_null_run requires an index= "
                "declaration — without one, row order and therefore 'consecutive' are undefined"
            )
        if extra not in {"ignore", "forbid"}:
            raise ValueError("Contract extra must be 'ignore' or 'forbid'")
        if on_violation not in {"fail", "warn"}:
            raise ValueError("Contract on_violation must be 'fail' or 'warn'")
        if min_rows is not None and (isinstance(min_rows, bool) or not isinstance(min_rows, int) or min_rows < 1):
            raise ValueError("Contract min_rows must be a positive integer or None")
        self.columns = list(columns)
        self.primary_key = primary_key
        self.min_rows = min_rows
        self.extra = extra
        self.on_violation = on_violation
        self.watermark_column = watermark_column
        self.index = index
        self.require_contract = bool(require_contract)

    def to_dict(self) -> dict[str, Any]:
        x_rebase: dict[str, Any] = {
            "primary_key": list(self.primary_key),
            "extra": self.extra,
            "on_violation": self.on_violation,
            "require_contract": self.require_contract,
        }
        if self.min_rows is not None:
            x_rebase["min_rows"] = self.min_rows
        if self.watermark_column is not None:
            x_rebase["watermark_column"] = self.watermark_column
        if self.index is not None:
            x_rebase["index"] = self.index.to_dict()
        return {
            "$schema": CONTRACT_SCHEMA,
            "properties": {column.name: column.to_property() for column in self.columns},
            "required": [column.name for column in self.columns if column.not_null],
            "x-rebase": x_rebase,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Contract:
        """Parse the stored contract form back into a Contract (unknown keys ignored)."""
        if not isinstance(data, dict):
            raise TypeError("Contract.from_dict expects a dict")
        properties = data.get("properties") or {}
        required = set(data.get("required") or [])
        columns = [
            Column.from_property(name, prop if isinstance(prop, dict) else {}, required=name in required)
            for name, prop in properties.items()
        ]
        x_rebase = data.get("x-rebase") or {}
        return cls(
            columns,
            primary_key=tuple(x_rebase.get("primary_key") or ()),
            min_rows=x_rebase.get("min_rows"),
            extra=x_rebase.get("extra", "ignore"),
            on_violation=x_rebase.get("on_violation", "fail"),
            watermark_column=x_rebase.get("watermark_column"),
            index=Index.from_dict(x_rebase["index"]) if x_rebase.get("index") else None,
            require_contract=bool(x_rebase.get("require_contract", False)),
        )


class Freshness:
    """How recently a dataset must have been signalled, and when to check."""

    _MAX_AGE_PATTERN = r"^\d+\s*(s|m|h|d)?$"

    def __init__(self, max_age: str | int | float | timedelta, *, check_at: Any = None) -> None:
        import re

        if isinstance(max_age, timedelta):
            max_age = f"{int(max_age.total_seconds())}s"
        elif isinstance(max_age, bool):
            raise TypeError("Freshness max_age must be a duration string, seconds, or timedelta")
        elif isinstance(max_age, (int, float)):
            max_age = f"{int(max_age)}s"
        elif isinstance(max_age, str):
            max_age = max_age.strip()
            if not re.match(self._MAX_AGE_PATTERN, max_age):
                raise ValueError("Freshness max_age must look like '45m', '2h', '1d' or '90s'")
        else:
            raise TypeError("Freshness max_age must be a duration string, seconds, or timedelta")

        if check_at is not None:
            if hasattr(check_at, "to_dict"):
                check_at = check_at.to_dict()
            if not isinstance(check_at, dict) or check_at.get("type") != "cron":
                raise TypeError("Freshness check_at must be rb.Cron(...) or a cron schedule dictionary")
        self.max_age = max_age
        self.check_at = check_at

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"max_age": self.max_age}
        if self.check_at is not None:
            payload["check_at"] = dict(self.check_at)
        return payload


@dataclass
class CheckFailure:
    """One failed contract check, with a few offending row positions."""

    check: str
    column: str | None
    count: int
    sample_rows: list[int] = field(default_factory=list)
    detail: str = ""


@dataclass
class ValidationReport:
    """Outcome of validating one frame against a dataset contract."""

    passed: bool
    checks: int
    row_count: int
    failures: list[CheckFailure] = field(default_factory=list)
    skipped: bool = False

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "passed": self.passed,
            "checks": self.checks,
            "row_count": self.row_count,
            "failures": [
                {
                    "check": failure.check,
                    "column": failure.column,
                    "count": failure.count,
                    "sample_rows": [int(row) for row in failure.sample_rows[:MAX_SAMPLE_ROWS]],
                    "detail": failure.detail,
                }
                for failure in self.failures[:MAX_REPORT_FAILURES]
            ],
        }
        if self.skipped:
            payload["skipped"] = True
        return payload


def violation_message(dataset_name: str, report: ValidationReport, *, nothing_written: bool = False) -> str:
    """Compose the human-facing ContractViolation message for a failed report."""
    violating = sum(failure.count for failure in report.failures)
    name = dataset_name or "frame"
    lines = [
        f"{name} failed {len(report.failures)} of {report.checks} checks ({violating:,} of {report.row_count:,} rows)",
        "",
    ]
    for failure in report.failures:
        target = f" · {failure.column}" if failure.column else ""
        lines.append(f"  {failure.check}{target}: {failure.detail}")
        if failure.sample_rows:
            shown = ", ".join(str(row) for row in failure.sample_rows[:MAX_SAMPLE_ROWS])
            suffix = ", ..." if failure.count > len(failure.sample_rows[:MAX_SAMPLE_ROWS]) else ""
            lines.append(f"      rows {shown}{suffix}")
    if nothing_written:
        lines.append("")
        lines.append(
            'Nothing was written. Set on_violation="warn" on the contract to write anyway and flag the signal.'
        )
    return "\n".join(lines)


@dataclass
class Check:
    """One compiled contract check: a named predicate over a whole frame."""

    check: str
    column: str | None
    run: Callable[[Any], CheckFailure | None]


def _sample_positions(mask: Any) -> list[int]:
    positions = mask.to_numpy().nonzero()[0][:MAX_SAMPLE_ROWS]
    return [int(position) for position in positions]


def _dtype_matches(series: Any, dtype: str) -> bool:
    from pandas.api import types as pdt

    if dtype == "timestamp":
        return bool(pdt.is_datetime64_any_dtype(series))
    if dtype == "date":
        if pdt.is_datetime64_any_dtype(series):
            return True
        if series.dtype == object:
            values = series.dropna()
            return bool(len(values) == 0 or values.map(lambda v: isinstance(v, _date)).all())
        return False
    if dtype == "float":
        return bool(pdt.is_float_dtype(series) or pdt.is_numeric_dtype(series))
    if dtype == "int":
        return bool(pdt.is_integer_dtype(series))
    if dtype == "string":
        return bool(pdt.is_string_dtype(series) or series.dtype == object)
    if dtype == "bool":
        return bool(pdt.is_bool_dtype(series))
    return False


def _property_dtype_label(prop: dict[str, Any]) -> str:
    for dtype, spec in _DTYPE_TO_PROPERTY.items():
        if prop.get("type") == spec["type"] and prop.get("format") == spec.get("format"):
            return dtype
    return str(prop.get("type", "unknown"))


def _coerce_bound(series: Any, value: Any) -> Any:
    """Adapt a stored bound to the series dtype so comparisons behave (ISO string → Timestamp)."""
    import pandas as pd
    from pandas.api import types as pdt

    if pdt.is_datetime64_any_dtype(series):
        bound = pd.Timestamp(value)
        tz = getattr(series.dtype, "tz", None)
        if tz is not None and bound.tzinfo is None:
            bound = bound.tz_localize(tz)
        elif tz is None and bound.tzinfo is not None:
            bound = bound.tz_localize(None)
        return bound
    return value


def _zero_like(series: Any) -> Any:
    """The zero appropriate to ``series.diff()`` — Timedelta for temporal, 0 for numeric."""
    import pandas as pd
    from pandas.api import types as pdt

    if pdt.is_datetime64_any_dtype(series):
        return pd.Timedelta(0)
    return 0


def compile_checks(contract: dict[str, Any]) -> list[Check]:
    """Compile a stored contract dict into runnable checks."""
    if not isinstance(contract, dict):
        raise TypeError("contract must be a dict (use Contract(...).to_dict())")
    properties: dict[str, dict[str, Any]] = {
        name: prop if isinstance(prop, dict) else {} for name, prop in (contract.get("properties") or {}).items()
    }
    required = set(contract.get("required") or [])
    x_rebase = contract.get("x-rebase") or {}
    checks: list[Check] = []

    for name, prop in properties.items():
        not_null = name in required or bool(prop.get("x-not-null"))
        dtype = _property_dtype_label(prop)

        if not_null:
            checks.append(Check("missing_column", name, _make_missing_column_check(name)))
            checks.append(Check("not_null", name, _make_not_null_check(name)))
        checks.append(Check("dtype", name, _make_dtype_check(name, dtype)))
        if prop.get("minimum") is not None or prop.get("maximum") is not None:
            checks.append(Check("range", name, _make_range_check(name, prop.get("minimum"), prop.get("maximum"))))
        if prop.get("enum"):
            checks.append(Check("isin", name, _make_isin_check(name, list(prop["enum"]))))
        max_null_run = prop.get("x-max-null-run")
        if max_null_run is not None:
            checks.append(Check("null_run", name, _make_null_run_check(name, int(max_null_run))))

    primary_key = list(x_rebase.get("primary_key") or ())
    if primary_key:
        checks.append(Check("primary_key", None, _make_primary_key_check(primary_key)))
    index_spec = x_rebase.get("index") or {}
    index_column = index_spec.get("column")
    if index_column and index_spec.get("monotonic"):
        checks.append(Check("index_monotonic", index_column, _make_monotonic_check(index_column)))
    if index_column and index_spec.get("max_gap"):
        checks.append(
            Check("index_max_gap", index_column, _make_max_gap_check(index_column, str(index_spec["max_gap"])))
        )
    min_rows = x_rebase.get("min_rows")
    if min_rows is not None:
        checks.append(Check("min_rows", None, _make_min_rows_check(int(min_rows))))
    if x_rebase.get("extra") == "forbid":
        checks.append(Check("extra_columns", None, _make_extra_columns_check(set(properties))))
    return checks


def _make_missing_column_check(name: str) -> Callable[[Any], CheckFailure | None]:
    def run(df: Any) -> CheckFailure | None:
        if name in df.columns:
            return None
        return CheckFailure("missing_column", name, len(df), [], "required column missing")

    return run


def _make_not_null_check(name: str) -> Callable[[Any], CheckFailure | None]:
    def run(df: Any) -> CheckFailure | None:
        if name not in df.columns:
            return None  # the missing_column check reports the root cause
        mask = df[name].isna()
        count = int(mask.sum())
        if not count:
            return None
        return CheckFailure("not_null", name, count, _sample_positions(mask), f"{count} rows null")

    return run


def _make_dtype_check(name: str, dtype: str) -> Callable[[Any], CheckFailure | None]:
    def run(df: Any) -> CheckFailure | None:
        if name not in df.columns:
            return None
        series = df[name]
        if _dtype_matches(series, dtype):
            return None
        return CheckFailure("dtype", name, len(df), [], f"expected {dtype}, got {series.dtype}")

    return run


def _make_range_check(name: str, minimum: Any, maximum: Any) -> Callable[[Any], CheckFailure | None]:
    def run(df: Any) -> CheckFailure | None:
        if name not in df.columns:
            return None
        series = df[name]
        try:
            mask = series.notna() & False
            if minimum is not None:
                mask = mask | (series < _coerce_bound(series, minimum))
            if maximum is not None:
                mask = mask | (series > _coerce_bound(series, maximum))
            mask = mask & series.notna()  # nulls are not range violations; not_null covers them
            count = int(mask.sum())
            if not count:
                return None
            low = minimum if minimum is not None else "-inf"
            high = maximum if maximum is not None else "inf"
            detail = f"{count} rows outside [{low}, {high}]"
            offending = series[mask]
            if maximum is not None and (offending > _coerce_bound(series, maximum)).any():
                detail += f" (max seen {offending.max()})"
            elif minimum is not None:
                detail += f" (min seen {offending.min()})"
            return CheckFailure("range", name, count, _sample_positions(mask), detail)
        except Exception:  # wrong dtype etc. — the dtype check reports the root cause
            return None

    return run


def _make_isin_check(name: str, values: list[Any]) -> Callable[[Any], CheckFailure | None]:
    def run(df: Any) -> CheckFailure | None:
        if name not in df.columns:
            return None
        series = df[name]
        try:
            mask = ~series.isin(values) & series.notna()
            count = int(mask.sum())
            if not count:
                return None
            unexpected = sorted({str(value) for value in series[mask].head(5)})
            detail = f"{count} rows not in {values} (saw {', '.join(unexpected)})"
            return CheckFailure("isin", name, count, _sample_positions(mask), detail)
        except Exception:
            return None

    return run


def _make_null_run_check(name: str, max_run: int) -> Callable[[Any], CheckFailure | None]:
    def run(df: Any) -> CheckFailure | None:
        if name not in df.columns:
            return None  # the missing_column check reports the root cause
        try:
            nulls = df[name].isna()
            if not bool(nulls.any()):
                return None
            # Number each maximal run of nulls, then measure it: cumsum over the *starts* of
            # non-null stretches gives every consecutive null block a shared group id.
            groups = (~nulls).cumsum()
            lengths = nulls.groupby(groups).transform("sum")
            mask = nulls & (lengths > max_run)
            count = int(mask.sum())
            if not count:
                return None
            longest = int(lengths[nulls].max())
            detail = f"run of {longest} consecutive nulls exceeds {max_run} ({count} rows in over-long runs)"
            return CheckFailure("null_run", name, count, _sample_positions(mask), detail)
        except Exception:  # wrong dtype etc. — the dtype check reports the root cause
            return None

    return run


def _make_primary_key_check(primary_key: list[str]) -> Callable[[Any], CheckFailure | None]:
    def run(df: Any) -> CheckFailure | None:
        if any(key not in df.columns for key in primary_key):
            return None  # missing_column checks report the root cause
        mask = df.duplicated(subset=primary_key, keep=False)
        count = int(mask.sum())
        if not count:
            return None
        detail = f"{count} duplicate rows on ({', '.join(primary_key)})"
        return CheckFailure("primary_key", None, count, _sample_positions(mask), detail)

    return run


def _make_monotonic_check(name: str) -> Callable[[Any], CheckFailure | None]:
    def run(df: Any) -> CheckFailure | None:
        if name not in df.columns:
            return None  # the missing_column check reports the root cause
        try:
            series = df[name]
            # The first row has no predecessor and can never be a violation; positions where the
            # diff is non-positive are the offending row, not its predecessor.
            mask = series.diff() <= _zero_like(series)
            mask.iloc[0] = False
            count = int(mask.sum())
            if not count:
                return None
            return CheckFailure(
                "index_monotonic",
                name,
                count,
                _sample_positions(mask),
                f"{name} is not strictly increasing ({count} rows)",
            )
        except Exception:  # wrong dtype etc. — the dtype check reports the root cause
            return None

    return run


def _make_max_gap_check(name: str, max_gap: str) -> Callable[[Any], CheckFailure | None]:
    def run(df: Any) -> CheckFailure | None:
        if name not in df.columns:
            return None  # the missing_column check reports the root cause
        try:
            bound = Index(name, max_gap=max_gap).gap_timedelta()
            diffs = df[name].diff()
            mask = diffs > bound
            mask = mask.fillna(False)  # the first row has no predecessor
            count = int(mask.sum())
            if not count:
                return None
            widest = diffs[mask].max()
            detail = f"{count} gaps wider than {max_gap} (widest {widest})"
            return CheckFailure("index_max_gap", name, count, _sample_positions(mask), detail)
        except Exception:  # wrong dtype etc. — the dtype check reports the root cause
            return None

    return run


def _make_min_rows_check(min_rows: int) -> Callable[[Any], CheckFailure | None]:
    def run(df: Any) -> CheckFailure | None:
        if len(df) >= min_rows:
            return None
        return CheckFailure("min_rows", None, len(df), [], f"{len(df)} rows, expected at least {min_rows}")

    return run


def _make_extra_columns_check(declared: set[str]) -> Callable[[Any], CheckFailure | None]:
    def run(df: Any) -> CheckFailure | None:
        extra = [str(column) for column in df.columns if column not in declared]
        if not extra:
            return None
        detail = f"unexpected columns: {', '.join(extra)}"
        return CheckFailure("extra_columns", None, len(df), [], detail)

    return run


def validate_frame(df: Any, contract: dict[str, Any], *, dataset_name: str = "") -> ValidationReport:
    """Validate a pandas DataFrame against a stored contract dict."""
    checks = compile_checks(contract)
    failures = [failure for failure in (check.run(df) for check in checks) if failure is not None]
    return ValidationReport(
        passed=not failures,
        checks=len(checks),
        row_count=int(len(df)),
        failures=failures,
    )
