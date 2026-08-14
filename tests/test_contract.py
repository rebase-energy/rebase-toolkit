import importlib.util
from datetime import timedelta

import pytest

import rebase as rb
from rebase.contract import (
    CheckFailure,
    Column,
    Contract,
    ContractViolation,
    Freshness,
    Index,
    ValidationReport,
    compile_checks,
    validate_frame,
    violation_message,
)

_HAS_PANDAS = importlib.util.find_spec("pandas") is not None

pandas_only = pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")


# --- constructor validation -----------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"name": "", "dtype": "float"}, "non-empty name"),
        ({"name": "x", "dtype": "decimal"}, "dtype must be one of"),
        ({"name": "x", "dtype": "float", "between": (1, 10), "isin": [1]}, "at most one"),
        ({"name": "x", "dtype": "string", "between": ("a", "b")}, "between= only applies"),
        ({"name": "x", "dtype": "bool", "between": (0, 1)}, "between= only applies"),
        ({"name": "x", "dtype": "float", "between": (1,)}, "2-tuple"),
        ({"name": "x", "dtype": "float", "between": (None, None)}, "at least one bound"),
        ({"name": "x", "dtype": "float", "isin": [1.0]}, "isin= only applies"),
        ({"name": "x", "dtype": "bool", "isin": [True]}, "isin= only applies"),
        ({"name": "x", "dtype": "string", "isin": []}, "non-empty list"),
    ],
)
def test_column_constructor_rejects_bad_input(kwargs, match) -> None:
    name = kwargs.pop("name")
    dtype = kwargs.pop("dtype")
    with pytest.raises(ValueError, match=match):
        Column(name, dtype, **kwargs)


def test_column_open_bounds_are_allowed() -> None:
    assert Column("x", "float", between=(None, 100)).between == (None, 100)
    assert Column("x", "int", between=(0, None)).between == (0, None)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"columns": []}, "non-empty list"),
        ({"columns": ["not-a-column"]}, "Column instances"),
        ({"primary_key": ("missing",)}, "undeclared column"),
        ({"watermark_column": "missing"}, "not a declared column"),
        ({"extra": "reject"}, "'ignore' or 'forbid'"),
        ({"on_violation": "explode"}, "'fail' or 'warn'"),
        ({"min_rows": 0}, "positive integer"),
        ({"min_rows": True}, "positive integer"),
    ],
)
def test_contract_constructor_rejects_bad_input(kwargs, match) -> None:
    columns = kwargs.pop("columns", [Column("ts", "timestamp")])
    with pytest.raises((ValueError, TypeError), match=match):
        Contract(columns, **kwargs)


def test_contract_rejects_duplicate_column_names() -> None:
    with pytest.raises(ValueError, match="unique"):
        Contract([Column("a", "float"), Column("a", "int")])


@pytest.mark.parametrize(
    ("max_age", "error", "match"),
    [
        ("", ValueError, "max_age"),
        ("soon", ValueError, "max_age"),
        ("45x", ValueError, "max_age"),
        (True, TypeError, "max_age"),
        (object(), TypeError, "max_age"),
    ],
)
def test_freshness_rejects_bad_max_age(max_age, error, match) -> None:
    with pytest.raises(error, match=match):
        Freshness(max_age)


def test_freshness_rejects_non_cron_check_at() -> None:
    with pytest.raises(TypeError, match="check_at"):
        Freshness("45m", check_at={"type": "interval", "every": "1h"})
    with pytest.raises(TypeError, match="check_at"):
        Freshness("45m", check_at="15 9 * * *")


# --- serialisation --------------------------------------------------------------------


def _example_contract() -> Contract:
    return Contract(
        [
            Column("price_eur_mwh", "float", not_null=True, between=(-500, 4000)),
            Column("area", "string", not_null=True, isin=["SE1", "SE2", "SE3", "SE4"]),
            Column("delivery_start", "timestamp", not_null=True),
            Column("volume_mw", "float"),
        ],
        primary_key=("delivery_start", "area"),
        min_rows=1,
        watermark_column="delivery_start",
    )


def test_contract_to_dict_shape() -> None:
    assert _example_contract().to_dict() == {
        "$schema": "rebase/contract-v1",
        "properties": {
            "price_eur_mwh": {"type": "number", "minimum": -500, "maximum": 4000, "x-not-null": True},
            "area": {"type": "string", "enum": ["SE1", "SE2", "SE3", "SE4"], "x-not-null": True},
            "delivery_start": {"type": "string", "format": "date-time", "x-not-null": True},
            "volume_mw": {"type": "number"},
        },
        "required": ["price_eur_mwh", "area", "delivery_start"],
        "x-rebase": {
            "primary_key": ["delivery_start", "area"],
            "extra": "ignore",
            "on_violation": "fail",
            "require_contract": False,
            "min_rows": 1,
            "watermark_column": "delivery_start",
        },
    }


def test_contract_from_dict_round_trips() -> None:
    stored = _example_contract().to_dict()
    assert Contract.from_dict(stored).to_dict() == stored


def test_contract_from_dict_ignores_unknown_keys() -> None:
    stored = _example_contract().to_dict()
    stored["properties"]["volume_mw"]["x-unknown"] = "?"
    stored["x-rebase"]["future_policy"] = 42
    parsed = Contract.from_dict(stored)
    assert parsed.min_rows == 1
    assert {column.name for column in parsed.columns} == {"price_eur_mwh", "area", "delivery_start", "volume_mw"}


def test_freshness_to_dict_shapes() -> None:
    assert Freshness("45m").to_dict() == {"max_age": "45m"}
    assert Freshness(90).to_dict() == {"max_age": "90s"}
    assert Freshness(timedelta(hours=2)).to_dict() == {"max_age": "7200s"}
    assert Freshness("45m", check_at=rb.Cron("15 9 * * *", timezone="Europe/Stockholm")).to_dict() == {
        "max_age": "45m",
        "check_at": {
            "type": "cron",
            "cron": "15 9 * * *",
            "timezone": "Europe/Stockholm",
            "day_or": True,
            "active": True,
        },
    }


# --- validation engine ----------------------------------------------------------------


def _frame(**columns):
    import pandas as pd

    return pd.DataFrame(columns)


def _single_column_contract(column: Column, **kwargs) -> dict:
    return Contract([column], **kwargs).to_dict()


def _failure(report: ValidationReport, check: str) -> CheckFailure:
    matches = [failure for failure in report.failures if failure.check == check]
    assert matches, f"no {check!r} failure in {report.failures}"
    return matches[0]


@pandas_only
def test_not_null_check() -> None:
    contract = _single_column_contract(Column("v", "float", not_null=True))
    assert validate_frame(_frame(v=[1.0, 2.0]), contract).passed
    report = validate_frame(_frame(v=[1.0, None, None]), contract)
    failure = _failure(report, "not_null")
    assert failure.count == 2
    assert failure.sample_rows == [1, 2]


@pandas_only
def test_missing_required_column_check() -> None:
    contract = _single_column_contract(Column("v", "float", not_null=True))
    report = validate_frame(_frame(other=[1.0]), contract)
    failure = _failure(report, "missing_column")
    assert failure.column == "v"
    # the column's other checks are skipped: no not_null/dtype failures for 'v'
    assert {f.check for f in report.failures} == {"missing_column"}


@pandas_only
def test_missing_nullable_column_is_not_a_failure() -> None:
    contract = _single_column_contract(Column("v", "float"))
    assert validate_frame(_frame(other=[1.0]), contract).passed


@pandas_only
def test_range_check_excludes_nulls() -> None:
    contract = _single_column_contract(Column("v", "float", between=(0, 100)))
    assert validate_frame(_frame(v=[0.0, 100.0, None]), contract).passed
    report = validate_frame(_frame(v=[-1.0, 50.0, 250.5, None]), contract)
    failure = _failure(report, "range")
    assert failure.count == 2
    assert failure.sample_rows == [0, 2]
    assert "max seen 250.5" in failure.detail


@pandas_only
def test_range_check_open_bounds() -> None:
    low_only = _single_column_contract(Column("v", "float", between=(0, None)))
    assert validate_frame(_frame(v=[0.0, 1e9]), low_only).passed
    report = validate_frame(_frame(v=[-5.0]), low_only)
    assert _failure(report, "range").count == 1


@pandas_only
def test_isin_check_excludes_nulls() -> None:
    contract = _single_column_contract(Column("area", "string", isin=["SE1", "SE2"]))
    assert validate_frame(_frame(area=["SE1", None]), contract).passed
    report = validate_frame(_frame(area=["SE1", "NO1", None]), contract)
    failure = _failure(report, "isin")
    assert failure.count == 1
    assert failure.sample_rows == [1]
    assert "NO1" in failure.detail


@pandas_only
@pytest.mark.parametrize(
    ("dtype", "good", "bad"),
    [
        ("float", [1.5], ["a"]),
        ("int", [1], [1.5]),
        ("string", ["a"], [1.5]),
        ("bool", [True], ["yes"]),
    ],
)
def test_dtype_checks(dtype, good, bad) -> None:
    contract = _single_column_contract(Column("v", dtype))
    assert validate_frame(_frame(v=good), contract).passed
    report = validate_frame(_frame(v=bad), contract)
    assert "expected" in _failure(report, "dtype").detail


@pandas_only
def test_timestamp_dtype_accepts_tz_aware_and_naive() -> None:
    import pandas as pd

    contract = _single_column_contract(Column("ts", "timestamp"))
    assert validate_frame(_frame(ts=pd.to_datetime(["2026-07-11"], utc=True)), contract).passed
    assert validate_frame(_frame(ts=pd.to_datetime(["2026-07-11"])), contract).passed
    assert not validate_frame(_frame(ts=["2026-07-11"]), contract).passed


@pandas_only
def test_date_dtype_accepts_date_objects() -> None:
    from datetime import date

    contract = _single_column_contract(Column("d", "date"))
    assert validate_frame(_frame(d=[date(2026, 7, 11)]), contract).passed
    assert not validate_frame(_frame(d=[1.5]), contract).passed


@pandas_only
def test_int_dtype_passes_float_check_but_not_vice_versa() -> None:
    float_contract = _single_column_contract(Column("v", "float"))
    int_contract = _single_column_contract(Column("v", "int"))
    assert validate_frame(_frame(v=[1, 2]), float_contract).passed  # int is numeric
    assert not validate_frame(_frame(v=[1.5]), int_contract).passed


@pandas_only
def test_primary_key_check() -> None:
    contract = Contract(
        [Column("ts", "timestamp"), Column("area", "string")],
        primary_key=("ts", "area"),
    ).to_dict()
    import pandas as pd

    good = _frame(ts=pd.to_datetime(["2026-07-11", "2026-07-12"]), area=["SE1", "SE1"])
    assert validate_frame(good, contract).passed
    dup = _frame(ts=pd.to_datetime(["2026-07-11", "2026-07-11"]), area=["SE1", "SE1"])
    report = validate_frame(dup, contract)
    failure = _failure(report, "primary_key")
    assert failure.count == 2
    assert failure.sample_rows == [0, 1]


@pandas_only
def test_min_rows_check() -> None:
    contract = _single_column_contract(Column("v", "float"), min_rows=2)
    assert validate_frame(_frame(v=[1.0, 2.0]), contract).passed
    report = validate_frame(_frame(v=[1.0]), contract)
    assert "expected at least 2" in _failure(report, "min_rows").detail


@pandas_only
def test_extra_columns_check() -> None:
    ignore = _single_column_contract(Column("v", "float"))
    forbid = _single_column_contract(Column("v", "float"), extra="forbid")
    frame = _frame(v=[1.0], surprise=["x"])
    assert validate_frame(frame, ignore).passed
    report = validate_frame(frame, forbid)
    assert "surprise" in _failure(report, "extra_columns").detail


@pandas_only
def test_range_on_timestamp_column() -> None:
    import pandas as pd

    contract = _single_column_contract(
        Column("ts", "timestamp", between=("2026-01-01T00:00:00Z", "2026-12-31T00:00:00Z"))
    )
    inside = _frame(ts=pd.to_datetime(["2026-06-01"], utc=True))
    outside = _frame(ts=pd.to_datetime(["2027-06-01"], utc=True))
    assert validate_frame(inside, contract).passed
    assert not validate_frame(outside, contract).passed


@pandas_only
def test_compile_checks_counts() -> None:
    checks = compile_checks(_example_contract().to_dict())
    # 3 not_null columns -> missing_column + not_null each; 4 dtype; 1 range; 1 enum; pk; min_rows
    names = [check.check for check in checks]
    assert names.count("missing_column") == 3
    assert names.count("not_null") == 3
    assert names.count("dtype") == 4
    assert names.count("range") == 1
    assert names.count("isin") == 1
    assert "primary_key" in names
    assert "min_rows" in names
    assert "extra_columns" not in names  # extra="ignore"


# --- report + message -----------------------------------------------------------------


def test_report_payload_caps_failures_and_sample_rows() -> None:
    failures = [CheckFailure("range", f"col{i}", 100, sample_rows=list(range(25)), detail="boom") for i in range(30)]
    payload = ValidationReport(passed=False, checks=40, row_count=1000, failures=failures).to_payload()
    assert len(payload["failures"]) == 20
    assert all(len(failure["sample_rows"]) == 10 for failure in payload["failures"])
    assert "skipped" not in payload


def test_skipped_report_payload() -> None:
    payload = ValidationReport(passed=False, checks=0, row_count=7, skipped=True).to_payload()
    assert payload == {"passed": False, "checks": 0, "row_count": 7, "failures": [], "skipped": True}


def test_violation_message_contains_counts_and_rows() -> None:
    report = ValidationReport(
        passed=False,
        checks=6,
        row_count=3456,
        failures=[
            CheckFailure("range", "price", 3, [1042, 1043, 1051], "3 rows outside [-500, 4000]"),
            CheckFailure("not_null", "delivery_start", 11, [2200, 2201], "11 rows null"),
        ],
    )
    message = violation_message("nordpool/prices", report, nothing_written=True)
    assert "nordpool/prices failed 2 of 6 checks (14 of 3,456 rows)" in message
    assert "range · price: 3 rows outside [-500, 4000]" in message
    assert "rows 1042, 1043, 1051" in message
    assert "rows 2200, 2201, ..." in message  # more offenders than samples -> ellipsis
    assert message.endswith('Set on_violation="warn" on the contract to write anyway and flag the signal.')


def test_violation_message_without_write_trailer() -> None:
    report = ValidationReport(
        passed=False, checks=1, row_count=1, failures=[CheckFailure("min_rows", None, 0, [], "0 rows")]
    )
    message = violation_message("d", report)
    assert "Nothing was written" not in message


def test_contract_violation_is_a_rebase_error() -> None:
    error = ContractViolation("boom", report=ValidationReport(passed=False, checks=1, row_count=0))
    assert isinstance(error, rb.RebaseWorkflowError)
    assert isinstance(error, rb.ContractViolation)
    assert error.report is not None


# --- Dataset.validate -----------------------------------------------------------------


@pandas_only
def test_dataset_validate_uses_in_code_contract() -> None:
    dataset = rb.Dataset("nordpool/prices", contract=_example_contract())
    frame = _frame(
        price_eur_mwh=[10.0],
        area=["SE1"],
        delivery_start=__import__("pandas").to_datetime(["2026-07-11"], utc=True),
        volume_mw=[1.0],
    )
    report = dataset.validate(frame)
    assert report.passed is True


@pandas_only
def test_dataset_validate_raises_on_failure_when_asked() -> None:
    dataset = rb.Dataset("nordpool/prices", contract=_example_contract())
    frame = _frame(price_eur_mwh=[9999.0], area=["XX"], delivery_start=[None], volume_mw=[1.0])
    report = dataset.validate(frame)
    assert report.passed is False
    with pytest.raises(rb.ContractViolation, match="failed"):
        dataset.validate(frame, raise_on_failure=True)


def test_dataset_validate_without_contract_raises(monkeypatch) -> None:
    from rebase.client import Client

    monkeypatch.setattr(Client, "get_dataset", lambda self, name: {"name": name, "contract": None})
    dataset = rb.Dataset("nordpool/prices", client=Client(api_key="rbw_test", api_url="https://api.example.com"))
    with pytest.raises(ValueError, match="has no contract"):
        dataset.validate(object())


def test_dataset_coerces_contract_and_freshness_to_dicts() -> None:
    dataset = rb.Dataset(
        "nordpool/prices",
        contract=_example_contract(),
        freshness=Freshness("45m"),
    )
    assert isinstance(dataset.contract, dict)
    assert dataset.freshness == {"max_age": "45m"}
    with pytest.raises(TypeError, match="contract"):
        rb.Dataset("d", contract="not-a-dict")


# --- execution semantics server parity ------------------------------------------------


def test_execution_values_match_server_contract(monkeypatch) -> None:
    """The SDK's accepted execution values must match the API server.

    ``run_type`` constants remain exported only for older clients.
    """
    from rebase.client import EXECUTION_MODES, ISOLATIONS, RUN_TYPES, WORKFLOW_RUN_TYPES

    assert EXECUTION_MODES == ("interactive", "job")
    assert ISOLATIONS == ("shared", "dedicated")
    assert RUN_TYPES == ("quick", "quick_shared", "long")
    assert WORKFLOW_RUN_TYPES == ("quick", "long")


def test_registration_payload_sends_execution_semantics_not_backend(monkeypatch) -> None:
    observed: dict = {}

    def fake_request(method: str, path: str, **kwargs):
        observed["json"] = kwargs["json"]
        return {"id": "function-id", "name": "add"}

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "request", fake_request)

    client.register_function(
        project="math",
        name="add",
        source_code="def add():\n    return {}",
        entrypoint="add",
    )

    payload = observed["json"]
    assert payload["mode"] == "interactive"
    assert payload["isolation"] == "shared"
    assert "run_type" not in payload
    assert "execution_backend" not in payload


# --- index constraints ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"column": ""}, "non-empty column"),
        ({"column": "t"}, "must declare monotonic"),
        ({"column": "t", "max_gap": "P1M"}, "calendar"),
        ({"column": "t", "max_gap": "PT0S"}, "must be positive"),
        ({"column": "t", "max_gap": -60}, "must be positive"),
        ({"column": "t", "max_gap": "banana"}, "max_gap must be"),
    ],
)
def test_index_constructor_rejects_bad_input(kwargs, match) -> None:
    with pytest.raises(ValueError, match=match):
        Index(**kwargs)


def test_index_accepts_both_duration_grammars() -> None:
    assert Index("t", max_gap="PT1H").max_gap == "PT1H"
    assert Index("t", max_gap="1h").max_gap == "PT1H"
    assert Index("t", max_gap=3600).max_gap == "PT1H"
    assert Index("t", max_gap=timedelta(hours=1)).max_gap == "PT1H"


def test_index_gap_timedelta() -> None:
    assert Index("t", max_gap="PT1H").gap_timedelta() == timedelta(hours=1)
    assert Index("t", max_gap="P1D").gap_timedelta() == timedelta(days=1)
    assert Index("t", monotonic=True).gap_timedelta() is None


def test_index_strips_column_name() -> None:
    assert Index("  valid_time  ", monotonic=True).column == "valid_time"


def test_index_to_dict_omits_unset() -> None:
    assert Index("t", monotonic=True).to_dict() == {"column": "t", "monotonic": True}
    assert Index("t", max_gap="PT1H").to_dict() == {"column": "t", "max_gap": "PT1H"}
    assert Index("t", monotonic=True, max_gap="PT15M").to_dict() == {
        "column": "t",
        "monotonic": True,
        "max_gap": "PT15M",
    }


def test_index_from_dict_round_trips() -> None:
    stored = Index("t", monotonic=True, max_gap="PT1H").to_dict()
    assert Index.from_dict(stored).to_dict() == stored


def test_index_from_dict_ignores_unknown_keys() -> None:
    parsed = Index.from_dict({"column": "t", "monotonic": True, "x-future": 1})
    assert parsed.column == "t"
    assert parsed.monotonic is True
