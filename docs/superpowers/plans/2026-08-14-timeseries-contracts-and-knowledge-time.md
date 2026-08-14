# Time-series Contracts and Declared Knowledge Time Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make row order and spacing declarable in `rb.Contract`, and make a write's `knowledge_time` provenance a declared option instead of an implicit wall-clock stamp.

**Architecture:** Part A adds an `Index` vocabulary object plus a `Column.max_null_run` keyword to `rebase/contract.py`, compiled into three new check closures by the existing `compile_checks`. Because validation already runs inside `DataSource.write`, the new checks reach the write pipeline with no change to `rebase/sources/`. Part B adds a `KnowledgeTime` declaration to `rebase/sources/base.py` that stamps the frame before validation, and fixes the wall-clock stamp in `rebase/sources/energy.py`.

**Tech Stack:** Python ≥ 3.12, pandas (optional dep, imported lazily inside functions), pytest, ruff, uv.

**Spec:** `docs/superpowers/specs/2026-08-14-timeseries-contracts-and-knowledge-time-design.md`

## Global Constraints

- **pandas is never a core dependency.** Import it *inside* functions, never at module level. `rebase/contract.py` and `rebase/sources/base.py` must stay importable without pandas.
- **Never break the stored contract representation for inputs accepted today.** `config_diff` compares in-code contracts against stored ones and `preflight_datasets` blocks `deploy` on drift, so a changed serialisation shows up as phantom drift on untouched datasets.
- **Errors follow module convention:** `rebase/contract.py` raises `ValueError`/`TypeError`; `rebase/sources/` raises `DataSourceError`.
- **Test commands:** `uv run --no-sync pytest -q`. Expect exactly one pre-existing failure, `tests/test_client.py::test_project_deploy_registers_step_workflow_graph`. Anything beyond that is yours.
- **Lint:** `uv run --no-sync ruff check .` must be clean. Run `ruff format` on touched files **only** — a bare `ruff format .` reformats two files already unformatted on `master`.
- **`ty check` is not a gate** (~81 pre-existing diagnostics). Judge by added diagnostics only.
- **Line length:** ruff is configured for long lines in this repo; match the surrounding style rather than wrapping at 88.
- No version bump. No README changes.

---

## File Structure

| File | Change | Responsibility |
| :-- | :-- | :-- |
| `rebase/contract.py` | Modify | Add `Index`, `Column.max_null_run`, `Contract.index`, three check factories, `Freshness` duration convergence |
| `rebase/__init__.py:47-53, 88-95` | Modify | Export `Index` |
| `rebase/sources/base.py` | Modify | Add `KnowledgeTime`, stamp in `write()`, replay-bound warning |
| `rebase/sources/energy.py:153-191` | Modify | Replay-aware `_resolve_now()`, accept a resolved `knowledge_time` |
| `rebase/sources/__init__.py` | Modify | Export `KnowledgeTime` |
| `tests/test_contract.py` | Modify | Part A tests, new `# --- index constraints ---` banner |
| `tests/test_sources.py` | Modify | Part B tests |
| `tests/test_sources_energy.py` | Modify | `build_values_rows` replay tests |
| `CHANGELOG.md` | Modify | One entry per part under `## Unreleased` → `### Added` |

---

# Part A — row order and spacing constraints

### Task 1: The `Index` vocabulary object

**Files:**
- Modify: `rebase/contract.py` (insert after `Column`, before `Contract` at line 146)
- Test: `tests/test_contract.py`

**Interfaces:**
- Consumes: `Duration` from `rebase.timing` (`Duration.coerce(value, *, field_name)` accepts `str | timedelta | Duration` — **not** `int`/`float`, so a numeric pre-pass is required).
- Produces: `Index(column, *, monotonic=False, max_gap=None)` with attributes `.column: str`, `.monotonic: bool`, `.max_gap: str | None` (ISO-8601 or `None`), and `.gap_timedelta() -> timedelta | None`. Methods `to_dict() -> dict`, `from_dict(data) -> Index` (classmethod).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_contract.py`, importing `Index` in the existing `from rebase.contract import (...)` block (alphabetically after `Freshness`):

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_contract.py -k index -q`
Expected: FAIL with `ImportError: cannot import name 'Index' from 'rebase.contract'`.

- [ ] **Step 3: Implement `Index`**

Insert into `rebase/contract.py` immediately after `Column.from_property` ends (line 143) and before `class Contract`:

```python
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
```

Note the negative-`max_gap` case: `Duration.coerce(timedelta(seconds=-60))` yields `seconds=-60.0`, which the `days <= 0 and seconds <= 0` guard rejects with "must be positive". `"PT0S"` hits the same guard.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_contract.py -k index -q`
Expected: PASS (11 tests).

- [ ] **Step 5: Commit**

```bash
git add rebase/contract.py tests/test_contract.py
git commit -m "Add rb.Index for row-order and spacing constraints"
```

---

### Task 2: `Column.max_null_run`

**Files:**
- Modify: `rebase/contract.py:65-143` (`Column.__init__`, `to_property`, `from_property`)
- Test: `tests/test_contract.py`

**Interfaces:**
- Produces: `Column(..., max_null_run: int | None = None)` with attribute `.max_null_run`, serialised as `x-max-null-run` on the JSON Schema property.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize(
    ("value", "match"),
    [
        (0, "positive integer"),
        (-1, "positive integer"),
        (True, "positive integer"),
        (1.5, "positive integer"),
    ],
)
def test_column_max_null_run_rejects_bad_input(value, match) -> None:
    with pytest.raises(ValueError, match=match):
        Column("v", "float", max_null_run=value)


def test_column_max_null_run_serialises() -> None:
    assert Column("v", "float", max_null_run=3).to_property() == {"type": "number", "x-max-null-run": 3}
    assert "x-max-null-run" not in Column("v", "float").to_property()


def test_column_max_null_run_round_trips() -> None:
    prop = Column("v", "float", max_null_run=3).to_property()
    assert Column.from_property("v", prop).max_null_run == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_contract.py -k max_null_run -q`
Expected: FAIL with `TypeError: Column.__init__() got an unexpected keyword argument 'max_null_run'`.

- [ ] **Step 3: Implement**

In `rebase/contract.py`, add the keyword to `Column.__init__` after `isin` (line 72):

```python
        isin: list[Any] | None = None,
        max_null_run: int | None = None,
    ) -> None:
```

Add validation immediately before `self.name = name.strip()` (line 94):

```python
        if max_null_run is not None and (
            isinstance(max_null_run, bool) or not isinstance(max_null_run, int) or max_null_run < 1
        ):
            raise ValueError(f"Column {name!r}: max_null_run must be a positive integer or None")
```

Add the attribute after `self.isin = isin` (line 98):

```python
        self.max_null_run = max_null_run
```

In `to_property`, add before `return prop` (line 112):

```python
        if self.max_null_run is not None:
            prop["x-max-null-run"] = self.max_null_run
```

In `from_property`, extend the constructor call (lines 137-143):

```python
        return cls(
            name,
            dtype,
            not_null=bool(required or prop.get("x-not-null")),
            between=between,
            isin=isin,
            max_null_run=prop.get("x-max-null-run"),
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_contract.py -q`
Expected: PASS. The whole file runs to catch any regression in `test_contract_to_dict_shape`, which asserts exact dict equality.

- [ ] **Step 5: Commit**

```bash
git add rebase/contract.py tests/test_contract.py
git commit -m "Add Column.max_null_run"
```

---

### Task 3: Wire `index` into `Contract` and export it

**Files:**
- Modify: `rebase/contract.py:149-227` (`Contract.__init__`, `to_dict`, `from_dict`)
- Modify: `rebase/__init__.py:47-53` and `rebase/__init__.py:88-95`
- Test: `tests/test_contract.py`, `tests/test_public_api.py`

**Interfaces:**
- Consumes: `Index` (Task 1), `Column.max_null_run` (Task 2).
- Produces: `Contract(..., index: Index | None = None)` with attribute `.index`, serialised at `x-rebase["index"]`. `rb.Index` importable from the package root.

- [ ] **Step 1: Write the failing tests**

```python
def _indexed_contract() -> Contract:
    return Contract(
        [
            Column("valid_time", "timestamp", not_null=True),
            Column("value", "float", between=(0, 40_000), max_null_run=3),
            Column("backup", "float", max_null_run=12),
        ],
        primary_key=("valid_time",),
        index=Index(column="valid_time", monotonic=True, max_gap="PT1H"),
    )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"index": Index("nope", monotonic=True)}, "not a declared column"),
        ({"index": Index("label", max_gap="PT1H")}, "max_gap requires"),
        ({"index": Index("label", monotonic=True)}, "monotonic requires"),
    ],
)
def test_contract_index_validation(kwargs, match) -> None:
    columns = [Column("valid_time", "timestamp"), Column("label", "string")]
    with pytest.raises(ValueError, match=match):
        Contract(columns, **kwargs)


def test_contract_rejects_index_that_is_not_an_index() -> None:
    with pytest.raises(TypeError, match="rebase.Index"):
        Contract([Column("valid_time", "timestamp")], index={"column": "valid_time"})


def test_max_null_run_requires_a_declared_index() -> None:
    with pytest.raises(ValueError, match="max_null_run requires"):
        Contract([Column("valid_time", "timestamp"), Column("v", "float", max_null_run=3)])


def test_contract_index_to_dict_shape() -> None:
    assert _indexed_contract().to_dict()["x-rebase"]["index"] == {
        "column": "valid_time",
        "monotonic": True,
        "max_gap": "PT1H",
    }
    assert _indexed_contract().to_dict()["properties"]["value"]["x-max-null-run"] == 3


def test_contract_index_round_trips() -> None:
    stored = _indexed_contract().to_dict()
    assert Contract.from_dict(stored).to_dict() == stored


def test_contract_without_index_omits_the_key() -> None:
    assert "index" not in _example_contract().to_dict()["x-rebase"]


def test_index_is_exported() -> None:
    assert rb.Index is Index
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_contract.py -k "index or max_null_run" -q`
Expected: FAIL with `TypeError: Contract.__init__() got an unexpected keyword argument 'index'`.

- [ ] **Step 3: Implement**

In `Contract.__init__`, add the keyword after `watermark_column` (line 157):

```python
        watermark_column: str | None = None,
        index: Index | None = None,
        require_contract: bool = False,
```

Insert validation after the `watermark_column` check (line 174), where `known` is already in scope:

```python
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
                f"Contract column(s) {null_run_columns} declare max_null_run, which requires an index= "
                "declaration — without one, row order and therefore 'consecutive' are undefined"
            )
```

Add the attribute alongside the others (after line 186):

```python
        self.index = index
```

Add the two dtype sets next to `_ISIN_DTYPES` (line 39):

```python
_GAP_DTYPES = {"timestamp", "date"}
_MONOTONIC_DTYPES = {"timestamp", "date", "float", "int"}
```

In `to_dict`, add after the `watermark_column` block (line 199):

```python
        if self.index is not None:
            x_rebase["index"] = self.index.to_dict()
```

In `from_dict`, add to the constructor call (after line 225):

```python
            watermark_column=x_rebase.get("watermark_column"),
            index=Index.from_dict(x_rebase["index"]) if x_rebase.get("index") else None,
```

In `rebase/__init__.py`, add `Index` to the contract import block (line 47-53), alphabetically after `Freshness`:

```python
from rebase.contract import (
    Column,
    Contract,
    ContractViolation,
    Freshness,
    Index,
    ValidationReport,
)
```

and to `__all__`, between `"Freshness"` (line 92) and `"Layer"` (line 93):

```python
    "Index",
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_contract.py tests/test_public_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rebase/contract.py rebase/__init__.py tests/test_contract.py
git commit -m "Wire Contract(index=...) and export rb.Index"
```

---

### Task 4: The `index_monotonic` check

**Files:**
- Modify: `rebase/contract.py` (`compile_checks` at 391-423, plus a new factory)
- Test: `tests/test_contract.py`

**Interfaces:**
- Consumes: `x-rebase["index"]` from the stored dict (Task 3).
- Produces: a `Check("index_monotonic", <column>, run)` appended by `compile_checks`.

- [ ] **Step 1: Write the failing tests**

```python
def _index_checks_contract(**index_kwargs) -> dict:
    return Contract(
        [Column("t", "timestamp", not_null=True), Column("v", "float")],
        index=Index("t", **index_kwargs),
    ).to_dict()


@pandas_only
def test_monotonic_check_passes_on_increasing_index() -> None:
    import pandas as pd

    frame = _frame(t=pd.to_datetime(["2026-01-01T00:00Z", "2026-01-01T01:00Z"]), v=[1.0, 2.0])
    assert validate_frame(frame, _index_checks_contract(monotonic=True)).passed


@pandas_only
def test_monotonic_check_flags_out_of_order_and_duplicate_rows() -> None:
    import pandas as pd

    frame = _frame(
        t=pd.to_datetime(
            [
                "2026-01-01T00:00Z",
                "2026-01-01T02:00Z",
                "2026-01-01T01:00Z",  # position 2: goes backwards
                "2026-01-01T01:00Z",  # position 3: duplicate, so not increasing
            ]
        ),
        v=[1.0, 2.0, 3.0, 4.0],
    )
    report = validate_frame(frame, _index_checks_contract(monotonic=True))
    failure = _failure(report, "index_monotonic")
    assert failure.column == "t"
    assert failure.count == 2
    assert failure.sample_rows == [2, 3]
    assert "not strictly increasing" in failure.detail


@pandas_only
def test_monotonic_check_skips_when_index_column_missing() -> None:
    report = validate_frame(_frame(v=[1.0]), _index_checks_contract(monotonic=True))
    assert [failure.check for failure in report.failures if failure.column == "t"] == ["missing_column"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_contract.py -k monotonic -q`
Expected: FAIL — `_failure` asserts with "no 'index_monotonic' failure in [...]".

- [ ] **Step 3: Implement**

In `compile_checks`, insert after the `primary_key` block (line 417) and before `min_rows`:

```python
    index_spec = x_rebase.get("index") or {}
    index_column = index_spec.get("column")
    if index_column and index_spec.get("monotonic"):
        checks.append(Check("index_monotonic", index_column, _make_monotonic_check(index_column)))
```

Add the factory after `_make_primary_key_check` (line 520):

```python
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
```

`_zero_like` handles the fact that `diff()` on a datetime series yields timedeltas, which do not compare against the integer `0`. Add it next to `_coerce_bound` (line 375):

```python
def _zero_like(series: Any) -> Any:
    """The zero appropriate to ``series.diff()`` — Timedelta for temporal, 0 for numeric."""
    import pandas as pd
    from pandas.api import types as pdt

    if pdt.is_datetime64_any_dtype(series):
        return pd.Timedelta(0)
    return 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_contract.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rebase/contract.py tests/test_contract.py
git commit -m "Add the index_monotonic contract check"
```

---

### Task 5: The `index_max_gap` check

**Files:**
- Modify: `rebase/contract.py` (`compile_checks`, plus a new factory)
- Test: `tests/test_contract.py`

**Interfaces:**
- Consumes: `x-rebase["index"]["max_gap"]` (an ISO-8601 string), `Index.gap_timedelta` (Task 1), and the `_index_checks_contract(**index_kwargs) -> dict` helper defined in Task 4.
- Produces: a `Check("index_max_gap", <column>, run)`.

- [ ] **Step 1: Write the failing tests**

```python
@pandas_only
def test_max_gap_check_passes_on_regular_cadence() -> None:
    import pandas as pd

    frame = _frame(t=pd.date_range("2026-01-01", periods=24, freq="h", tz="UTC"), v=[1.0] * 24)
    assert validate_frame(frame, _index_checks_contract(max_gap="PT1H")).passed


@pandas_only
def test_max_gap_check_flags_a_hole() -> None:
    import pandas as pd

    frame = _frame(
        t=pd.to_datetime(
            [
                "2026-01-01T00:00Z",
                "2026-01-01T01:00Z",
                "2026-01-01T07:00Z",  # position 2: six hours after its predecessor
                "2026-01-01T08:00Z",
            ]
        ),
        v=[1.0, 2.0, 3.0, 4.0],
    )
    report = validate_frame(frame, _index_checks_contract(max_gap="PT1H"))
    failure = _failure(report, "index_max_gap")
    assert failure.column == "t"
    assert failure.count == 1
    assert failure.sample_rows == [2]
    assert "6:00:00" in failure.detail
    assert "PT1H" in failure.detail


@pandas_only
def test_max_gap_check_skips_when_index_column_missing() -> None:
    report = validate_frame(_frame(v=[1.0]), _index_checks_contract(max_gap="PT1H"))
    assert [failure.check for failure in report.failures if failure.column == "t"] == ["missing_column"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_contract.py -k max_gap -q`
Expected: FAIL — "no 'index_max_gap' failure in [...]".

- [ ] **Step 3: Implement**

In `compile_checks`, extend the index block added in Task 4:

```python
    index_spec = x_rebase.get("index") or {}
    index_column = index_spec.get("column")
    if index_column and index_spec.get("monotonic"):
        checks.append(Check("index_monotonic", index_column, _make_monotonic_check(index_column)))
    if index_column and index_spec.get("max_gap"):
        checks.append(
            Check("index_max_gap", index_column, _make_max_gap_check(index_column, str(index_spec["max_gap"])))
        )
```

Add the factory after `_make_monotonic_check`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_contract.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rebase/contract.py tests/test_contract.py
git commit -m "Add the index_max_gap contract check"
```

---

### Task 6: The `null_run` check

**Files:**
- Modify: `rebase/contract.py` (`compile_checks` per-column loop, plus a new factory)
- Test: `tests/test_contract.py`

**Interfaces:**
- Consumes: `x-max-null-run` on the column property (Task 2).
- Produces: a `Check("null_run", <column>, run)` appended inside the per-column loop.

- [ ] **Step 1: Write the failing tests**

```python
def _null_run_contract(**thresholds) -> dict:
    columns = [Column("t", "timestamp", not_null=True)]
    columns += [Column(name, "float", max_null_run=value) for name, value in thresholds.items()]
    return Contract(columns, index=Index("t", monotonic=True)).to_dict()


@pandas_only
def test_null_run_check_passes_at_the_threshold() -> None:
    import pandas as pd

    frame = _frame(
        t=pd.date_range("2026-01-01", periods=6, freq="h", tz="UTC"),
        v=[1.0, None, None, None, 2.0, 3.0],
    )
    assert validate_frame(frame, _null_run_contract(v=3)).passed


@pandas_only
def test_null_run_check_flags_a_long_run() -> None:
    import pandas as pd

    frame = _frame(
        t=pd.date_range("2026-01-01", periods=8, freq="h", tz="UTC"),
        v=[1.0, None, None, None, None, None, 2.0, 3.0],
    )
    report = validate_frame(frame, _null_run_contract(v=3))
    failure = _failure(report, "null_run")
    assert failure.column == "v"
    assert failure.count == 5
    assert failure.sample_rows == [1, 2, 3, 4, 5]
    assert "run of 5 consecutive nulls" in failure.detail
    assert "exceeds 3" in failure.detail


@pandas_only
def test_null_run_thresholds_are_per_column() -> None:
    import pandas as pd

    frame = _frame(
        t=pd.date_range("2026-01-01", periods=6, freq="h", tz="UTC"),
        v=[1.0, None, None, None, None, 2.0],
        backup=[1.0, None, None, None, None, 2.0],
    )
    report = validate_frame(frame, _null_run_contract(v=3, backup=12))
    assert [failure.column for failure in report.failures if failure.check == "null_run"] == ["v"]


@pandas_only
def test_null_run_check_counts_the_longest_run_in_the_detail() -> None:
    import pandas as pd

    frame = _frame(
        t=pd.date_range("2026-01-01", periods=9, freq="h", tz="UTC"),
        v=[None, None, None, None, 1.0, None, None, None, None],
    )
    report = validate_frame(frame, _null_run_contract(v=2))
    failure = _failure(report, "null_run")
    assert failure.count == 8
    assert "run of 4 consecutive nulls" in failure.detail


@pandas_only
def test_null_run_check_skips_when_column_missing() -> None:
    report = validate_frame(_frame(t=[1], other=[1.0]), _null_run_contract(v=3))
    assert not [failure for failure in report.failures if failure.check == "null_run"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_contract.py -k null_run -q`
Expected: FAIL — "no 'null_run' failure in [...]".

- [ ] **Step 3: Implement**

In `compile_checks`, inside the per-column loop, add after the `enum` block (line 413):

```python
        max_null_run = prop.get("x-max-null-run")
        if max_null_run is not None:
            checks.append(Check("null_run", name, _make_null_run_check(name, int(max_null_run))))
```

Add the factory after `_make_isin_check` (line 506):

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_contract.py -q`
Expected: PASS.

- [ ] **Step 5: Verify the full suite and lint**

```bash
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync ruff format rebase/contract.py tests/test_contract.py
```
Expected: only the known `test_project_deploy_registers_step_workflow_graph` failure; ruff clean.

- [ ] **Step 6: Commit**

```bash
git add rebase/contract.py tests/test_contract.py
git commit -m "Add the null_run contract check"
```

---

### Task 7: Converge `Freshness` onto the `Duration` grammar

**Files:**
- Modify: `rebase/contract.py:230-263` (`Freshness`)
- Test: `tests/test_contract.py`

**Interfaces:**
- Consumes: `Duration.coerce` from `rebase.timing`.
- Produces: `Freshness` accepting ISO-8601 durations in addition to everything it accepts today, with an unchanged `to_dict()` for every previously valid input.

- [ ] **Step 1: Write the failing tests**

```python
def test_freshness_preserves_todays_stored_representation() -> None:
    # config_diff compares stored contracts, so a changed representation would show up as
    # phantom drift on datasets nobody touched.
    assert Freshness("45m").to_dict() == {"max_age": "45m"}
    assert Freshness("2h").to_dict() == {"max_age": "2h"}
    assert Freshness("1d").to_dict() == {"max_age": "1d"}
    assert Freshness("90s").to_dict() == {"max_age": "90s"}
    assert Freshness("300").to_dict() == {"max_age": "300"}  # unitless: stored verbatim, as today
    assert Freshness(90).to_dict() == {"max_age": "90s"}
    assert Freshness(90.7).to_dict() == {"max_age": "90s"}
    assert Freshness(timedelta(hours=2)).to_dict() == {"max_age": "7200s"}


def test_freshness_now_accepts_iso_durations() -> None:
    assert Freshness("PT1H").to_dict() == {"max_age": "3600s"}
    assert Freshness("PT15M").to_dict() == {"max_age": "900s"}
    assert Freshness("P1D").to_dict() == {"max_age": "86400s"}


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ("P1M", "calendar"),
        ("-PT1H", "positive"),
        ("PT0S", "positive"),
        ("banana", "max_age must be"),
    ],
)
def test_freshness_rejects_bad_durations(value, match) -> None:
    with pytest.raises(ValueError, match=match):
        Freshness(value)


def test_freshness_still_rejects_bools() -> None:
    with pytest.raises(TypeError, match="max_age must be"):
        Freshness(True)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_contract.py -k freshness -q`
Expected: FAIL — `Freshness("PT1H")` raises `ValueError: Freshness max_age must look like '45m', ...`, and `Freshness("300")` currently stores `"300"`, not `"300s"`.

- [ ] **Step 3: Implement**

Replace `Freshness.__init__` (lines 235-257) and the `_MAX_AGE_PATTERN` class attribute (line 233) with:

```python
    # Strings already accepted before Freshness delegated to the Duration grammar. These keep
    # their exact stored representation — including a unitless "300" — because config_diff
    # compares stored contracts and a changed representation reads as drift on datasets nobody
    # touched. Unchanged from the pattern this class has always used.
    _LEGACY_PATTERN = r"^\d+\s*(s|m|h|d)?$"

    def __init__(self, max_age: str | int | float | timedelta | Any, *, check_at: Any = None) -> None:
        import re

        from rebase.timing import Duration

        if isinstance(max_age, bool):
            raise TypeError("Freshness max_age must be a duration string, seconds, or timedelta")
        elif isinstance(max_age, timedelta):
            max_age = f"{int(max_age.total_seconds())}s"
        elif isinstance(max_age, (int, float)):
            max_age = f"{int(max_age)}s"
        elif isinstance(max_age, str):
            max_age = max_age.strip()
            if re.match(self._LEGACY_PATTERN, max_age):
                pass  # already in the stored form; leave it byte-identical
            else:
                duration = Duration.coerce(max_age, field_name="Freshness max_age")
                if duration.months:
                    raise ValueError(
                        "Freshness max_age cannot be a calendar duration; a month has no fixed length"
                    )
                total = duration.days * 86400 + duration.seconds
                if total <= 0:
                    raise ValueError("Freshness max_age must be positive")
                max_age = f"{int(total)}s"
        else:
            raise TypeError("Freshness max_age must be a duration string, seconds, or timedelta")

        if check_at is not None:
            if hasattr(check_at, "to_dict"):
                check_at = check_at.to_dict()
            if not isinstance(check_at, dict) or check_at.get("type") != "cron":
                raise TypeError("Freshness check_at must be rb.Cron(...) or a cron schedule dictionary")
        self.max_age = max_age
        self.check_at = check_at
```

This is a strict superset: `_LEGACY_PATTERN` is byte-for-byte the pattern the class already used, so every string it accepted before still short-circuits and stores unchanged — including a unitless `"300"`, which stays `"300"` rather than being tidied into `"300s"`. Normalising it would be an improvement in isolation and a phantom-drift bug in practice, so it waits for a deliberate migration. Only strings the old pattern *rejected* reach `Duration.coerce`, and it raises `ValueError` reading `"Freshness max_age must be ..."`, which is what the test matches on.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_contract.py tests/test_cli.py tests/test_dataset_registry.py -q`
Expected: PASS. Those last two exercise `Freshness` through `dataset freshness` and `config_diff`.

- [ ] **Step 5: Commit**

```bash
git add rebase/contract.py tests/test_contract.py
git commit -m "Accept ISO-8601 durations in Freshness max_age"
```

---

### Task 8: Changelog for Part A

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add the entry**

Insert as the first bullet under `## Unreleased` → `### Added`, matching the file's hand-written prose style:

```markdown
- **`rb.Contract` can now constrain row order and spacing, not just column values.** An
  `index=rb.Index(column=..., monotonic=True, max_gap="PT1H")` declaration asserts that the
  index is strictly increasing and that no two consecutive rows sit further apart than the
  given duration, and `rb.Column(..., max_null_run=3)` bounds the run of consecutive nulls in
  one column. For time-series datasets that covers the most common failure class — a missing
  publication, a duplicated or out-of-order timestamp, a stretch of nulls where a feed dropped
  out — which previously had to be checked in a separate pass after the data had already
  landed. Because these compile into the same engine as the existing checks, they run
  before the write: a batch arriving with a six-hour hole in it fails validation, so
  `on_violation="fail"` refuses the write and `OnUpdate(only_valid=True)` never fires
  downstream work. Gaps are measured between consecutive rows as delivered, since sorting
  first would quietly repair a frame that failed the monotonicity assertion. `max_gap` accepts
  both duration grammars (`"PT1H"` and `"1h"`), and `rb.Freshness` now accepts ISO-8601
  durations too, so the two duration fields on a contract no longer disagree.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "Changelog: contract index constraints"
```

---

# Part B — declared knowledge time

### Task 9: The `KnowledgeTime` declaration

**Files:**
- Modify: `rebase/sources/base.py` (insert after `BitemporalSpec`, before `_replay_knowledge_time` at line 94)
- Modify: `rebase/sources/__init__.py:23-28, 60-70`
- Test: `tests/test_sources.py`

**Interfaces:**
- Produces: `KnowledgeTime` with classmethods `from_source(column: str)`, `from_inputs(*frames)`, `at(moment: datetime)`, and instance method `apply(df: Frame) -> Frame` returning a **copy** with a `knowledge_time` column. Exported as `rb.sources.KnowledgeTime`.

- [ ] **Step 1: Write the failing tests**

Add `KnowledgeTime` to the existing `from rebase.sources.base import (...)` block in `tests/test_sources.py` (alphabetically after `DataSourceError`), then:

```python
# --- declared knowledge time ----------------------------------------------------------


def test_knowledge_time_constructors_validate() -> None:
    with pytest.raises(DataSourceError, match="non-empty column"):
        KnowledgeTime.from_source("")
    with pytest.raises(DataSourceError, match="at least one input"):
        KnowledgeTime.from_inputs()
    with pytest.raises(DataSourceError, match="requires a datetime"):
        KnowledgeTime.at("2026-01-01")


def test_knowledge_time_at_warns_on_naive_datetime() -> None:
    with pytest.warns(UserWarning, match="timezone-naive"):
        KnowledgeTime.at(datetime(2026, 1, 1, 9, 0))


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_knowledge_time_from_source_stamps_the_column() -> None:
    import pandas as pd

    df = pd.DataFrame(
        {
            "valid_time": pd.to_datetime(["2026-01-01T00:00Z", "2026-01-01T01:00Z"]),
            "issued_at": pd.to_datetime(["2026-01-01T00:05Z", "2026-01-01T01:05Z"]),
            "value": [1.0, 2.0],
        }
    )
    out = KnowledgeTime.from_source("issued_at").apply(df)
    assert list(out["knowledge_time"]) == list(pd.to_datetime(["2026-01-01T00:05Z", "2026-01-01T01:05Z"]))
    assert "knowledge_time" not in df.columns  # the caller's frame is untouched


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_knowledge_time_from_source_rejects_missing_column_and_nulls() -> None:
    import pandas as pd

    with pytest.raises(DataSourceError, match="not found in frame columns"):
        KnowledgeTime.from_source("issued_at").apply(pd.DataFrame({"value": [1.0]}))
    df = pd.DataFrame({"issued_at": pd.to_datetime(["2026-01-01T00:05Z", None]), "value": [1.0, 2.0]})
    with pytest.raises(DataSourceError, match="1 rows have no publication time"):
        KnowledgeTime.from_source("issued_at").apply(df)


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_knowledge_time_from_inputs_takes_the_max() -> None:
    import pandas as pd

    actuals = pd.DataFrame({"knowledge_time": pd.to_datetime(["2026-01-01T00:00Z", "2026-01-01T06:00Z"])})
    weather = pd.DataFrame({"knowledge_time": pd.to_datetime(["2026-01-01T03:00Z"])})
    out = KnowledgeTime.from_inputs(actuals, weather).apply(pd.DataFrame({"value": [1.0, 2.0]}))
    assert set(out["knowledge_time"]) == {pd.Timestamp("2026-01-01T06:00Z")}
    assert len(out) == 2


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_knowledge_time_from_inputs_names_the_offending_input() -> None:
    import pandas as pd

    good = pd.DataFrame({"knowledge_time": pd.to_datetime(["2026-01-01T00:00Z"])})
    bad = pd.DataFrame({"value": [1.0]})
    with pytest.raises(DataSourceError, match="input 1 has no knowledge_time"):
        KnowledgeTime.from_inputs(good, bad).apply(pd.DataFrame({"value": [1.0]}))


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_knowledge_time_at_stamps_a_scalar() -> None:
    import pandas as pd

    moment = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    out = KnowledgeTime.at(moment).apply(pd.DataFrame({"value": [1.0, 2.0]}))
    assert set(out["knowledge_time"]) == {pd.Timestamp(moment)}


def test_knowledge_time_is_exported() -> None:
    assert rb.sources.KnowledgeTime is KnowledgeTime
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_sources.py -k knowledge_time -q`
Expected: FAIL with `ImportError: cannot import name 'KnowledgeTime' from 'rebase.sources.base'`.

- [ ] **Step 3: Implement**

Insert into `rebase/sources/base.py` after `BitemporalSpec.__post_init__` (line 91) and before `_replay_knowledge_time`:

```python
class KnowledgeTime:
    """Where a write's ``knowledge_time`` comes from — the write-side counterpart to
    :class:`BitemporalSpec`.

    Writing to a store with a knowledge axis has one rule that is easy to get wrong and wrong
    silently: ``knowledge_time`` must record when the data became *knowable*, not when you
    fetched it. Stamp wall-clock instead and an upstream revision becomes indistinguishable
    from a re-fetch of unchanged data — which is exactly the signal a data-quality layer needs
    to tell noise from a real correction.

    There is deliberately no ``now()`` constructor. Wall-clock is the failure mode, and
    omitting ``knowledge_time=`` from :meth:`DataSource.write` already leaves the frame alone.
    """

    __slots__ = ("_column", "_inputs", "_moment")

    def __init__(self, *, column: str | None = None, inputs: tuple = (), moment: datetime | None = None) -> None:
        # Construct through the classmethods below; they are the documented surface.
        self._column = column
        self._inputs = tuple(inputs)
        self._moment = moment

    @classmethod
    def from_source(cls, column: str) -> KnowledgeTime:
        """Take ``knowledge_time`` from the upstream's publication-time column."""
        if not isinstance(column, str) or not column.strip():
            raise DataSourceError("KnowledgeTime.from_source requires a non-empty column name")
        return cls(column=column.strip())

    @classmethod
    def from_inputs(cls, *frames: Frame) -> KnowledgeTime:
        """For a derived series: ``max(knowledge_time)`` across the frames it was computed from.

        Anything earlier would claim the derived value was knowable before its inputs were,
        and any backtest reading through it would leak.
        """
        if not frames:
            raise DataSourceError("KnowledgeTime.from_inputs requires at least one input frame")
        return cls(inputs=frames)

    @classmethod
    def at(cls, moment: datetime) -> KnowledgeTime:
        """An explicit knowledge time."""
        if not isinstance(moment, datetime):
            raise DataSourceError("KnowledgeTime.at requires a datetime")
        if moment.tzinfo is None:
            warnings.warn("knowledge_time is timezone-naive; assuming UTC", stacklevel=2)
            moment = moment.replace(tzinfo=UTC)
        return cls(moment=moment)

    def apply(self, df: Frame) -> Frame:
        """Return a copy of ``df`` with ``knowledge_time`` stamped. Never mutates ``df``."""
        import pandas as pd

        out = df.copy()
        if self._column is not None:
            if self._column not in out.columns:
                raise DataSourceError(
                    f"KnowledgeTime.from_source({self._column!r}): column not found in frame columns "
                    f"{list(out.columns)}."
                )
            values = pd.to_datetime(out[self._column], utc=True)
            missing = int(values.isna().sum())
            if missing:
                raise DataSourceError(
                    f"KnowledgeTime.from_source({self._column!r}): {missing} rows have no publication time. "
                    "A null publication time is not a knowledge time — fix the upstream or filter those rows."
                )
            out["knowledge_time"] = values
            return out
        if self._inputs:
            out["knowledge_time"] = self._max_input_knowledge_time()
            return out
        out["knowledge_time"] = pd.Timestamp(self._moment)
        return out

    def _max_input_knowledge_time(self) -> Any:
        import pandas as pd

        moments = []
        for position, frame in enumerate(self._inputs):
            columns = getattr(frame, "columns", None)
            if columns is None or "knowledge_time" not in columns:
                raise DataSourceError(
                    f"KnowledgeTime.from_inputs: input {position} has no knowledge_time column. "
                    "Read it with read_bitemporal so the knowledge axis travels with the frame."
                )
            values = pd.to_datetime(frame["knowledge_time"], utc=True)
            if not len(values) or bool(values.isna().all()):
                raise DataSourceError(
                    f"KnowledgeTime.from_inputs: input {position} has no usable knowledge_time values."
                )
            moments.append(values.max())
        return max(moments)
```

Then export it. In `rebase/sources/__init__.py`, extend the import block (lines 23-28):

```python
from rebase.sources.base import (
    BitemporalSpec,
    DataSource,
    DataSourceError,
    KnowledgeTime,
    WriteResult,
)
```

and `__all__` (lines 60-70), between `"DataSourceError"` and `"SeriesKey"`:

```python
    "KnowledgeTime",
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_sources.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rebase/sources/base.py rebase/sources/__init__.py tests/test_sources.py
git commit -m "Add KnowledgeTime for declared write-side knowledge provenance"
```

---

### Task 10: Stamp `knowledge_time` in `DataSource.write`

**Files:**
- Modify: `rebase/sources/base.py:245-276` (`write` signature, docstring, and the pre-dataset block)
- Test: `tests/test_sources.py`

**Interfaces:**
- Consumes: `KnowledgeTime.apply` (Task 9).
- Produces: `DataSource.write(..., knowledge_time: KnowledgeTime | None = None)`. The stamp lands before contract resolution, so validation sees it.

- [ ] **Step 1: Write the failing tests**

```python
class _CapturingSource(_FakeSource):
    """A fake source that keeps the frame it was handed, so stamping is observable."""

    def __init__(self) -> None:
        super().__init__()
        self.written = None

    def _write(self, df, table, mode):
        self.written = df
        return super()._write(df, table, mode)


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_write_stamps_declared_knowledge_time() -> None:
    import pandas as pd

    source = _CapturingSource()
    df = pd.DataFrame({"issued_at": pd.to_datetime(["2026-01-01T00:05Z", "2026-01-01T01:05Z"]), "v": [1.0, 2.0]})
    source.write(df, "t", knowledge_time=KnowledgeTime.from_source("issued_at"))
    assert list(source.written["knowledge_time"]) == list(
        pd.to_datetime(["2026-01-01T00:05Z", "2026-01-01T01:05Z"])
    )
    assert "knowledge_time" not in df.columns


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_write_without_knowledge_time_leaves_the_frame_alone() -> None:
    import pandas as pd

    source = _CapturingSource()
    df = pd.DataFrame({"v": [1.0, 2.0]})
    source.write(df, "t")
    assert list(source.written.columns) == ["v"]


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_knowledge_time_stamp_is_visible_to_validation() -> None:
    import pandas as pd

    contract = rb.Contract(
        [
            rb.Column("v", "float", not_null=True),
            rb.Column("knowledge_time", "timestamp", not_null=True),
        ]
    ).to_dict()
    dataset = _RecordingDataset(contract=contract)
    source = _CapturingSource()
    df = pd.DataFrame({"issued_at": pd.to_datetime(["2026-01-01T00:05Z"]), "v": [1.0]})
    # Without the stamp the contract's required knowledge_time column is missing, so this
    # passing proves the stamp landed before validation ran.
    result = source.write(df, "t", dataset=dataset, knowledge_time=KnowledgeTime.from_source("issued_at"))
    assert result.validation.passed
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_sources.py -k "stamps or leaves_the_frame or visible_to_validation" -q`
Expected: FAIL with `TypeError: write() got an unexpected keyword argument 'knowledge_time'`.

- [ ] **Step 3: Implement**

In `rebase/sources/base.py`, add the keyword to `write` after `validate` (line 254):

```python
        validate: bool = True,
        knowledge_time: KnowledgeTime | None = None,
    ) -> WriteResult:
```

Append to the docstring, after the `validate=False` sentence (line 268):

```
        ``knowledge_time`` declares where the frame's knowledge axis comes from — see
        :class:`KnowledgeTime`. It is stamped *before* validation, so a contract may declare
        ``knowledge_time`` as a column, and the caller's frame is never mutated.
```

Insert the stamp at the top of the body, replacing line 270:

```python
        if knowledge_time is not None:
            df = knowledge_time.apply(df)
        replay_bound = _replay_knowledge_time()
```

That placement puts it ahead of the `if dataset is None:` early return, because provenance is independent of contracts.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_sources.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rebase/sources/base.py tests/test_sources.py
git commit -m "Stamp declared knowledge_time before contract validation"
```

---

### Task 11: Warn when a replay's knowledge time runs past the replay bound

**Files:**
- Modify: `rebase/sources/base.py` (`write`, plus a module-level helper)
- Test: `tests/test_sources.py`

**Interfaces:**
- Consumes: `_replay_knowledge_time` (existing), `KnowledgeTime.apply` (Task 9), the stamp placement from Task 10.
- Produces: a warning on the `rebase.sources` logger; the write still proceeds.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_replay_warns_when_knowledge_time_passes_the_replay_bound(monkeypatch, caplog) -> None:
    import logging

    import pandas as pd

    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", _REPLAY_BOUND)
    source = _CapturingSource()
    later = pd.Timestamp(_REPLAY_BOUND) + pd.Timedelta(hours=3)
    df = pd.DataFrame({"issued_at": [later], "v": [1.0]})
    with caplog.at_level(logging.WARNING, logger="rebase.sources"):
        source.write(df, "t", knowledge_time=KnowledgeTime.from_source("issued_at"))
    assert any("later than the replay bound" in record.getMessage() for record in caplog.records)
    assert source.writes == 1  # a diagnostic, not a reason to fail the job


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_replay_is_quiet_when_knowledge_time_is_within_the_bound(monkeypatch, caplog) -> None:
    import logging

    import pandas as pd

    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", _REPLAY_BOUND)
    source = _CapturingSource()
    earlier = pd.Timestamp(_REPLAY_BOUND) - pd.Timedelta(hours=3)
    df = pd.DataFrame({"issued_at": [earlier], "v": [1.0]})
    with caplog.at_level(logging.WARNING, logger="rebase.sources"):
        source.write(df, "t", knowledge_time=KnowledgeTime.from_source("issued_at"))
    assert not any("later than the replay bound" in record.getMessage() for record in caplog.records)
```

`_REPLAY_BOUND = "2026-07-10T09:00:00+00:00"` already exists at `tests/test_sources.py:347` — reuse it, don't redefine it. `logging` is imported inside each test body, matching the file's existing convention (see `tests/test_sources.py:250`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_sources.py -k replay_bound -q`
Expected: FAIL on the first test's `assert any(...)` — no such warning is emitted.

- [ ] **Step 3: Implement**

Add the helper to `rebase/sources/base.py` after `_resolve_now` (line 121):

```python
def _warn_if_past_replay_bound(df: Frame, bound: datetime, table: str) -> None:
    """Flag a replay writing data the original run could not have known.

    A declared knowledge time is *data*, so a replay must not overwrite it. But if it resolves
    past the replay bound, the replay is inventing knowledge the original run did not have —
    worth saying out loud, and not worth failing a job over mid-pipeline.
    """
    import pandas as pd

    try:
        latest = pd.to_datetime(df["knowledge_time"], utc=True).max()
    except Exception:  # noqa: BLE001 - a diagnostic must never break the write
        return
    if latest is None or pd.isna(latest):
        return
    if latest > pd.Timestamp(bound):
        _logger.warning(
            "replay run: knowledge_time %s in the frame written to %r is later than the replay bound %s — "
            "the replay is writing data the original run could not have known",
            latest.isoformat(),
            table,
            bound.isoformat(),
        )
```

In `write`, extend the stamp block from Task 10:

```python
        if knowledge_time is not None:
            df = knowledge_time.apply(df)
        replay_bound = _replay_knowledge_time()
        if knowledge_time is not None and replay_bound is not None:
            _warn_if_past_replay_bound(df, replay_bound, table)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_sources.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rebase/sources/base.py tests/test_sources.py
git commit -m "Warn when a replay's knowledge_time passes the replay bound"
```

---

### Task 12: Make `build_values_rows` replay-aware and knowledge-time-aware

**Files:**
- Modify: `rebase/sources/energy.py:38` (import), `rebase/sources/energy.py:153-191` (`build_values_rows`)
- Test: `tests/test_sources_energy.py`

**Interfaces:**
- Consumes: `_resolve_now` from `rebase.sources.base`.
- Produces: `build_values_rows(..., knowledge_time: Any | None = None)`. `knowledge_time` and `change_time` both fall back to `_resolve_now()` rather than wall-clock.

- [ ] **Step 1: Write the failing tests**

```python
_REPLAY_BOUND = "2026-07-10T09:00:00+00:00"


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_build_values_rows_stamps_the_replay_bound(monkeypatch) -> None:
    import pandas as pd

    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", _REPLAY_BOUND)
    frame = pd.DataFrame(
        {"valid_time": pd.to_datetime(["2026-07-10T00:00Z"]), "value": [1.0]}
    )
    rows = build_values_rows(frame, SeriesKey("p", "actual", "electricity.load"))
    assert rows["knowledge_time"].iloc[0] == pd.Timestamp(_REPLAY_BOUND)
    assert rows["change_time"].iloc[0] == pd.Timestamp(_REPLAY_BOUND)


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_build_values_rows_honours_a_passed_knowledge_time() -> None:
    import pandas as pd

    frame = pd.DataFrame(
        {"valid_time": pd.to_datetime(["2026-07-10T00:00Z"]), "value": [1.0]}
    )
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
```

`SeriesKey`, `datetime`, `UTC` and `pytest` are already imported at the top of `tests/test_sources_energy.py`. `build_values_rows` is not — add it to the existing import block (alphabetically after `SeriesKey`):

```python
from rebase.sources.energy import (
    SERIES_VALUES_COLUMNS,
    SeriesKey,
    build_values_rows,
    series_values_select,
)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_sources_energy.py -k "replay_bound or knowledge_time" -q`
Expected: FAIL — the first test stamps wall-clock rather than the replay bound; the second raises `TypeError: build_values_rows() got an unexpected keyword argument 'knowledge_time'`.

- [ ] **Step 3: Implement**

In `rebase/sources/energy.py`, extend the base import (line 38):

```python
from rebase.sources.base import DataSourceError, Frame, _replay_knowledge_time, _resolve_now
```

Add the keyword to `build_values_rows` after `run_id` (line 160):

```python
    run_id: int | None = None,
    knowledge_time: Any | None = None,
) -> Frame:
```

Update the docstring (lines 162-167):

```python
    """Expand a normalized series frame into full ``series_values`` insert rows.

    Stamps ``knowledge_time`` (from the frame if present, else ``knowledge_time=``, else the
    batch clock — SIMPLE shape), ``change_time`` (batch clock, always: corrections are new
    rows) and one ``run_id`` per batch, matching timedb's write defaults.

    The batch clock is :func:`_resolve_now`, not wall-clock, so a replay stamps the replay's
    knowledge-time bound. Stamping wall-clock here would record when you *fetched* rather than
    when the data became knowable, which is the one signal that distinguishes a genuine
    upstream correction from a re-fetch of unchanged data.
    """
```

Replace the `now` line (line 173) and the `knowledge_time` frame entry (line 179):

```python
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
```

Precedence is deliberate and matches the existing shape contract: a `knowledge_time` column in the frame is the VERSIONED shape and per-row, so it wins over a batch-level argument, which in turn wins over the batch clock.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_sources_energy.py tests/test_sources.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rebase/sources/energy.py tests/test_sources_energy.py
git commit -m "Make build_values_rows replay-aware and accept a declared knowledge_time"
```

---

### Task 13: Changelog for Part B and final verification

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add the entry**

Insert directly after the Part A bullet under `## Unreleased` → `### Added`:

```markdown
- **A write's `knowledge_time` can now be declared rather than assumed.**
  `rb.sources.KnowledgeTime.from_source("issued_at")` takes the knowledge axis from the
  upstream's publication-time column, and `rb.sources.KnowledgeTime.from_inputs(a, b)` gives a
  derived series `max(knowledge_time)` of the frames it was computed from — anything earlier
  would claim the derived value was knowable before its inputs were, and a backtest reading
  through it would leak. `KnowledgeTime.at(...)` covers the explicit case. The stamp is applied
  before validation, so a contract can require the column, and the caller's frame is never
  mutated. There is deliberately no `now()`: stamping wall-clock records when you *fetched*,
  which makes an upstream revision indistinguishable from a re-fetch of unchanged data —
  precisely the signal a data-quality layer needs. Resolution is strict, so a missing
  publication column or a null publication time is an error rather than a silent fallback.
  Relatedly, `build_values_rows` in the canonical energy layout now stamps
  `knowledge_time`/`change_time` from the replay-aware batch clock instead of wall-clock, so a
  replay records the original run's knowledge bound.
```

- [ ] **Step 2: Run the full verification**

```bash
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync ruff format rebase/contract.py rebase/sources/base.py rebase/sources/energy.py rebase/sources/__init__.py rebase/__init__.py tests/test_contract.py tests/test_sources.py tests/test_sources_energy.py
uv run --no-sync ruff check .
```

Expected: exactly one failure, `tests/test_client.py::test_project_deploy_registers_step_workflow_graph` (pre-existing on `master`). Ruff clean. If `ruff format` changes anything, re-run `pytest -q` before committing.

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "Changelog: declared knowledge time"
```

- [ ] **Step 4: Confirm no unrelated files drifted**

```bash
git status --short
git diff --stat master...HEAD
```
Expected: only the files listed in [File Structure](#file-structure), plus the two docs files from the spec commit.

---

## Deferred — not in this plan

Tracked for the follow-up branch, with reasoning in the spec's Sequencing section:

- `rb.OnNull` (never replace a stored real value with a null from a later fetch)
- `rb.Change` (declared change threshold with **reported** suppressions)
- `rb.sources.energydb()` and the `series=` kwarg on `write()`

All three need a read-before-write against a bitemporal store, which no existing connector can answer.
