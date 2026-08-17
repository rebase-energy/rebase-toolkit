# EnergyDB Bucket Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist and read back Rebase's canonical EnergyDB time-series layout through `rb.Bucket`, with declared write semantics, in a shape that could be loaded straight into a real EnergyDB later.

**Architecture:** An append-only object log. Every write puts a new immutable parquet object under a `series_id` / `valid_month` prefix; nothing is ever rewritten. Reads list the relevant prefixes, concatenate, and apply winner selection in pandas from a definition shared with the existing SQL builder. Write semantics (`OnNull`, `skip_unchanged`, `Change`) are applied by a read-before-write that is fail-open and always reports what it suppressed.

**Tech Stack:** Python ≥ 3.12, pandas + pyarrow (a new optional extra), `rb.Bucket`, pytest, ruff, uv.

**Spec:** `docs/superpowers/specs/2026-08-17-energydb-bucket-store-design.md`

## Global Constraints

- **This is a deliberately transitional implementation.** It is intended to be replaced by proper EnergyDB database support shaped like the other four connectors. `rebase/sources/energydb.py`'s module docstring and the CHANGELOG entry must both say so. Keep all bucket-specific machinery in `energydb.py`; keep dialect-neutral semantics in `energy.py`. Replacing the backing should mean replacing one file.
- **pandas is never a core dependency.** Import it *inside* functions, never at module level. `rebase/sources/energy.py` and `rebase/sources/energydb.py` must both stay importable without pandas.
- **`rebase/sources/` raises `DataSourceError`**, never bare `ValueError`/`RuntimeError`.
- **Objects hold exactly `SERIES_VALUES_COLUMNS`, in that order**, with parquet-preserved dtypes (`datetime64[us, UTC]`, `int64`, `float64`). This is the property that makes a future load into a real EnergyDB a load rather than a transform. Do not "improve" the on-disk format.
- **`series_id` is never exposed on a read.** Results carry `path` / `data_type` / `name`.
- **Suppression is fail-open.** Any exception in the suppression path writes the unfiltered batch and sets `fail_open=True`. Suppression is an optimisation, never a gate.
- **`Change.tolerance()` is absolute only.** No relative variant, ever, without an explicit new decision — a relative band is the recorded production defect.
- ruff line-length is 120. Match surrounding style; do not wrap at 88.
- No version bump.
- **Measured test baseline before this plan:** `uv run --no-sync pytest -q` → `4 failed, 917 passed`. The four are pre-existing platform failures: `tests/test_cli.py::test_profile_list_renders_local_profiles`, `tests/test_editor.py::test_editor_uses_a_macos_app_bundle_when_no_shim_is_on_path`, and two in `tests/test_tui.py`. Anything else is yours.
- **Known pre-existing test-order pollution, not yours:** running `tests/test_sources_energy.py` *before* `tests/test_sources.py` makes `test_factories_are_exported` fail (an in-test-body `from rebase.sources.bigquery import ...` shadows the factory attribute). Order the other way, or run them separately.

---

## File Structure

| File | Change | Responsibility |
| :-- | :-- | :-- |
| `rebase/sources/energy.py` | Modify | Dialect-neutral layer. Gains `_winner_rows`, `select_series_winners`, `select_current_state`, `series_keys`, `attach_series_keys`, `OnNull`, `Change`, `SeriesWriteResult`, `suppress_rows`. |
| `rebase/sources/energydb.py` | **Create** | Bucket-backed store: key layout, month pruning, parquet IO, `EnergyDBStore`. Carries the transitional-status docstring. |
| `rebase/sources/bigquery.py` | Modify | Imports the two hoisted helpers instead of defining them. Behaviour unchanged. |
| `rebase/sources/__init__.py` | Modify | `energydb()` factory plus new exports. |
| `pyproject.toml` | Modify | New `energydb` extra; add to `sources` and `all`. |
| `pypi/rebase/pyproject.toml` | Modify | Alias package forwards the new extra. |
| `tests/test_packaging.py` | Modify | The extras tuple is an exact-equality assertion. |
| `tests/test_sources_energy.py` | Modify | Tests for everything added to `energy.py`. |
| `tests/test_energydb.py` | **Create** | Tests for the store, with the in-memory `_FakeBucket`. |
| `CHANGELOG.md` | Modify | One entry, stating transitional status. |

---

### Task 1: Winner selection in pandas

**Files:**
- Modify: `rebase/sources/energy.py` (add after `series_values_select`, at end of file)
- Test: `tests/test_sources_energy.py`

**Interfaces:**
- Consumes: `_replay_knowledge_time` (already imported at `energy.py:39`), `DataSourceError`, `Frame`.
- Produces: `_winner_rows(df, *, partition: list[str], order: list[str]) -> Frame` (private) and `select_series_winners(df, *, overlapping: bool = False, include_updates: bool = False, as_of: datetime | None = None) -> Frame`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sources_energy.py`. Add `select_series_winners` to the existing `from rebase.sources.energy import (...)` block.

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_sources_energy.py -k winners -q`
Expected: FAIL with `ImportError: cannot import name 'select_series_winners' from 'rebase.sources.energy'`.

- [ ] **Step 3: Implement**

Append to `rebase/sources/energy.py`:

```python
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
    columns = ["series_id", "valid_time", "knowledge_time", "value"] if overlapping else ["series_id", "valid_time", "value"]
    winners = winners[columns]
    if len(winners):
        winners = winners.sort_values(["series_id", "valid_time"], kind="stable")
    return winners.reset_index(drop=True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_sources_energy.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rebase/sources/energy.py tests/test_sources_energy.py
git commit -m "Add pandas winner selection mirroring series_values_select"
```

---

### Task 2: Hoist the series-key helpers out of bigquery.py

**Files:**
- Modify: `rebase/sources/energy.py` (append)
- Modify: `rebase/sources/bigquery.py:20-25` (imports), `:231-234` (the remap), `:242-256` (delete `_series_keys`)
- Test: `tests/test_sources_energy.py`

**Interfaces:**
- Produces: `series_keys(keys: Any) -> list[SeriesKey]` and `attach_series_keys(df: Frame, by_id: dict[int, SeriesKey]) -> Frame`.

Both the BigQuery connector and the new store need these. This is a behaviour-preserving move; `tests/test_sources_energy.py`'s existing BigQuery tests are the guard.

- [ ] **Step 1: Write the failing tests**

```python
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
    frame = pd.DataFrame({"series_id": [key.series_id], "valid_time": [pd.Timestamp("2026-01-01T00:00Z")], "value": [1.0]})
    out = attach_series_keys(frame, {key.series_id: key})
    assert list(out.columns) == ["path", "data_type", "name", "valid_time", "value"]
    assert out["path"].iloc[0] == "p"
    assert "series_id" in frame.columns  # the caller's frame is untouched
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_sources_energy.py -k "series_keys or attach_series" -q`
Expected: FAIL with `ImportError: cannot import name 'series_keys'`.

- [ ] **Step 3: Implement**

Append to `rebase/sources/energy.py`:

```python
def series_keys(keys: Any) -> list[SeriesKey]:
    """Normalise one or many series keys into a list of :class:`SeriesKey`."""
    # Wrap single keys and non-iterables, so a scalar reaches the per-item check below and is
    # rejected with DataSourceError rather than raising TypeError from being iterated. `tuple`
    # must stay in the explicit set or a 3-tuple key would be iterated into three strings, and
    # `str` is there so the error names the string rather than a character. Real iterables —
    # list, set, generator — fall through and are treated as collections of keys.
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
    # A bare KeyError from the .map() below would violate this package's contract that every
    # failure surfaces as DataSourceError. An empty frame yields an empty set and so cannot
    # raise here, which read_series against an empty store relies on.
    missing = set(df["series_id"]) - set(by_id)
    if missing:
        raise DataSourceError(f"by_id must cover every series_id in the frame; missing {sorted(missing)!r}")
    out = df.copy()
    out.insert(0, "name", out["series_id"].map(lambda sid: by_id[sid].name))
    out.insert(0, "data_type", out["series_id"].map(lambda sid: by_id[sid].data_type))
    out.insert(0, "path", out["series_id"].map(lambda sid: by_id[sid].path))
    return out.drop(columns=["series_id"])
```

In `rebase/sources/bigquery.py`, extend the energy import (currently lines 20-25) to include `attach_series_keys` and `series_keys`, keeping alphabetical order:

```python
from rebase.sources.energy import (
    SERIES_CATALOG_COLUMNS,
    SeriesKey,
    attach_series_keys,
    build_values_rows,
    series_keys,
    series_values_select,
)
```

Replace `resolved = _series_keys(keys)` in `read_series` with `resolved = series_keys(keys)`.

Replace the four remap lines at the end of `read_series`:

```python
        df = self.read(sql, params=params)
        return attach_series_keys(df, by_id)
```

Delete the now-unused `_series_keys` function from `bigquery.py` (its body moved verbatim).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_sources.py tests/test_sources_energy.py -q`
Expected: PASS. `tests/test_sources_energy.py`'s existing `read_series` tests cover the BigQuery path and must stay green — that is what proves the move preserved behaviour.

- [ ] **Step 5: Commit**

```bash
git add rebase/sources/energy.py rebase/sources/bigquery.py tests/test_sources_energy.py
git commit -m "Hoist series_keys and the id remap into energy.py"
```

---

### Task 3: The `OnNull` and `Change` declarations

**Files:**
- Modify: `rebase/sources/energy.py` (append)
- Test: `tests/test_sources_energy.py`

**Interfaces:**
- Produces: `OnNull` (enum, members `KEEP_STORED` / `WRITE_NULL`), `Change` with `exact()`, `tolerance(atol)` and `values_equal(new, old) -> bool`, `SeriesWriteResult` (frozen dataclass), `MAX_SAMPLE_VALID_TIMES = 10`.

- [ ] **Step 1: Write the failing tests**

```python
# --- write-semantics declarations -----------------------------------------------------


def test_on_null_members() -> None:
    assert OnNull.KEEP_STORED.value == "keep_stored"
    assert OnNull.WRITE_NULL.value == "write_null"


def test_change_exact_compares_exactly() -> None:
    change = Change.exact()
    assert change.values_equal(1.0, 1.0)
    assert not change.values_equal(1.0, 1.0000001)


def test_change_treats_nan_as_equal_to_nan() -> None:
    # timedb's native comparison does the same; the live application implementation does
    # not, which is why it rewrites every null row on every pass.
    assert Change.exact().values_equal(float("nan"), float("nan"))
    assert Change.exact().values_equal(None, float("nan"))
    assert not Change.exact().values_equal(float("nan"), 1.0)
    assert not Change.exact().values_equal(1.0, None)


def test_change_tolerance_is_absolute() -> None:
    change = Change.tolerance(1e-6)
    assert change.values_equal(1.0, 1.0000001)
    # The recorded production defect: a relative band would call this unchanged on a
    # 5000-magnitude series. An absolute 1e-6 must not.
    assert not change.values_equal(5000.0, 5000.5)


def test_change_tolerance_boundary_is_inclusive() -> None:
    assert Change.tolerance(0.5).values_equal(10.0, 10.5)
    assert not Change.tolerance(0.5).values_equal(10.0, 10.6)


@pytest.mark.parametrize("bad", [0, -1, -0.5])
def test_change_tolerance_rejects_non_positive(bad) -> None:
    with pytest.raises(DataSourceError, match="atol > 0"):
        Change.tolerance(bad)


@pytest.mark.parametrize("bad", [True, "1e-6", None])
def test_change_tolerance_rejects_non_numbers(bad) -> None:
    with pytest.raises(DataSourceError, match="requires a number"):
        Change.tolerance(bad)


def test_series_write_result_shape() -> None:
    key = SeriesKey("p", "actual", "electricity.load")
    result = SeriesWriteResult(
        series=key,
        rows_written=3,
        objects_written=("a.parquet",),
        suppressed_unchanged=1,
        suppressed_null=2,
        sample_valid_times=("2026-01-01T00:00:00+00:00",),
    )
    assert result.fail_open is False
    assert result.rows_written == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_sources_energy.py -k "on_null or change_ or series_write_result" -q`
Expected: FAIL with `ImportError: cannot import name 'OnNull'`.

- [ ] **Step 3: Implement**

Add `from enum import Enum` to `energy.py`'s module-level imports (after `from dataclasses import dataclass`), then append:

```python
MAX_SAMPLE_VALID_TIMES = 10


class OnNull(Enum):
    """What a write does when the incoming value is null and a value is already stored."""

    KEEP_STORED = "keep_stored"
    """Never replace a stored real value with a null — a transient upstream gap must not destroy data."""

    WRITE_NULL = "write_null"
    """Treat the null as a real observation and record the gap."""


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
        """True when the two values count as unchanged. NaN equals NaN, matching timedb."""
        import math

        new_missing = new is None or (isinstance(new, float) and math.isnan(new))
        old_missing = old is None or (isinstance(old, float) and math.isnan(old))
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_sources_energy.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rebase/sources/energy.py tests/test_sources_energy.py
git commit -m "Add OnNull, Change and SeriesWriteResult declarations"
```

---

### Task 4: The suppression rules

**Files:**
- Modify: `rebase/sources/energy.py` (append)
- Test: `tests/test_sources_energy.py`

**Interfaces:**
- Consumes: `_winner_rows` (Task 1), `OnNull` / `Change` / `MAX_SAMPLE_VALID_TIMES` (Task 3).
- Produces: `select_current_state(df, *, overlapping: bool = False, as_of: datetime | None = None) -> Frame` returning winner rows with `value`, `annotation` and `changed_by` retained; and `suppress_rows(batch, stored, *, on_null=OnNull.KEEP_STORED, skip_unchanged=False, change=None, overlapping=False) -> tuple[Frame, dict[str, Any]]`.

`select_current_state` exists because the public read projection drops `annotation` and `changed_by`, but equality needs them.

The report dict has keys `suppressed_unchanged: int`, `suppressed_null: int`, `sample_valid_times: tuple[str, ...]`.

- [ ] **Step 1: Write the failing tests**

```python
# --- suppression rules ----------------------------------------------------------------


def _batch(rows):
    """(valid_time, value, annotation, changed_by) -> a canonical batch frame."""
    import pandas as pd

    records = [
        {
            "series_id": 7,
            "valid_time": pd.Timestamp(valid_time),
            "knowledge_time": pd.Timestamp("2026-02-01T00:00Z"),
            "change_time": pd.Timestamp("2026-02-01T00:00Z"),
            "value": value,
            "valid_time_end": pd.Timestamp("2200-01-01T00:00Z"),
            "run_id": 2,
            "changed_by": changed_by,
            "annotation": annotation,
            "retention": "forever",
        }
        for valid_time, value, annotation, changed_by in rows
    ]
    return pd.DataFrame.from_records(records, columns=_SV_COLUMNS)


def _stored(rows):
    """(valid_time, value, annotation, changed_by) -> a stored-state frame."""
    import pandas as pd

    records = [
        {
            "series_id": 7,
            "valid_time": pd.Timestamp(valid_time),
            "value": value,
            "annotation": annotation,
            "changed_by": changed_by,
        }
        for valid_time, value, annotation, changed_by in rows
    ]
    return pd.DataFrame.from_records(
        records, columns=["series_id", "valid_time", "value", "annotation", "changed_by"]
    )


_T0 = "2026-01-01T00:00Z"

_RULES = [
    # (label, stored rows, batch rows, on_null, skip_unchanged, expected kept valid_times)
    ("1 absent stored", [], [(_T0, 1.0, "", "")], OnNull.KEEP_STORED, True, [_T0]),
    ("2 null over null", [(_T0, float("nan"), "", "")], [(_T0, float("nan"), "", "")], OnNull.KEEP_STORED, True, []),
    ("3 real over null", [(_T0, float("nan"), "", "")], [(_T0, 5.0, "", "")], OnNull.KEEP_STORED, True, [_T0]),
    ("4 null over real, keep", [(_T0, 5.0, "", "")], [(_T0, float("nan"), "", "")], OnNull.KEEP_STORED, True, []),
    ("4 null over real, write", [(_T0, 5.0, "", "")], [(_T0, float("nan"), "", "")], OnNull.WRITE_NULL, True, [_T0]),
    ("5 fully equal", [(_T0, 5.0, "a", "u")], [(_T0, 5.0, "a", "u")], OnNull.KEEP_STORED, True, []),
    ("6 annotation differs", [(_T0, 5.0, "a", "u")], [(_T0, 5.0, "b", "u")], OnNull.KEEP_STORED, True, [_T0]),
    ("6 changed_by differs", [(_T0, 5.0, "a", "u")], [(_T0, 5.0, "a", "v")], OnNull.KEEP_STORED, True, [_T0]),
    ("7 value differs", [(_T0, 5.0, "a", "u")], [(_T0, 6.0, "a", "u")], OnNull.KEEP_STORED, True, [_T0]),
    # rules 5-7 do not run when skip_unchanged is False
    ("equal, no skip_unchanged", [(_T0, 5.0, "a", "u")], [(_T0, 5.0, "a", "u")], OnNull.KEEP_STORED, False, [_T0]),
]


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
@pytest.mark.parametrize(("label", "stored", "batch", "on_null", "skip_unchanged", "expected"), _RULES)
def test_suppression_rules(label, stored, batch, on_null, skip_unchanged, expected) -> None:
    import pandas as pd

    kept, _report = suppress_rows(
        _batch(batch), _stored(stored), on_null=on_null, skip_unchanged=skip_unchanged, change=Change.exact()
    )
    assert list(kept["valid_time"]) == [pd.Timestamp(value) for value in expected], label


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_suppression_still_protects_nulls_without_skip_unchanged() -> None:
    kept, report = suppress_rows(
        _batch([(_T0, float("nan"), "", "")]),
        _stored([(_T0, 5.0, "", "")]),
        on_null=OnNull.KEEP_STORED,
        skip_unchanged=False,
    )
    assert len(kept) == 0
    assert report["suppressed_null"] == 1
    assert report["suppressed_unchanged"] == 0


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_suppression_counts_and_samples() -> None:
    stored = _stored([(_T0, 5.0, "", ""), ("2026-01-01T01:00Z", 6.0, "", "")])
    batch = _batch([(_T0, 5.0, "", ""), ("2026-01-01T01:00Z", float("nan"), "", "")])
    kept, report = suppress_rows(batch, stored, skip_unchanged=True, change=Change.exact())
    assert len(kept) == 0
    assert report["suppressed_unchanged"] == 1
    assert report["suppressed_null"] == 1
    assert len(report["sample_valid_times"]) == 2


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_suppression_samples_are_capped() -> None:
    rows = [(f"2026-01-01T{hour:02d}:00Z", 1.0, "", "") for hour in range(15)]
    kept, report = suppress_rows(_batch(rows), _stored(rows), skip_unchanged=True, change=Change.exact())
    assert len(kept) == 0
    assert len(report["sample_valid_times"]) == MAX_SAMPLE_VALID_TIMES


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_suppression_bypassed_entirely_for_overlapping() -> None:
    # Every publication of a forecast is meaningful; a republication at a new knowledge_time
    # with an identical value must survive.
    stored = _stored([(_T0, 5.0, "", "")])
    kept, report = suppress_rows(
        _batch([(_T0, 5.0, "", "")]), stored, skip_unchanged=True, change=Change.exact(), overlapping=True
    )
    assert len(kept) == 1
    assert report["suppressed_unchanged"] == 0


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_suppression_with_empty_stored_state_keeps_everything() -> None:
    kept, report = suppress_rows(_batch([(_T0, 1.0, "", "")]), _stored([]), skip_unchanged=True, change=Change.exact())
    assert len(kept) == 1
    assert report["suppressed_unchanged"] == 0


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_select_current_state_retains_annotation_and_changed_by() -> None:
    frame = _values_frame([(_T0, "2026-01-01T06:00Z", "2026-01-01T06:00Z", 1.0)])
    out = select_current_state(frame)
    assert list(out.columns) == ["series_id", "valid_time", "value", "annotation", "changed_by"]
    assert len(out) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_sources_energy.py -k "suppression or current_state" -q`
Expected: FAIL with `ImportError: cannot import name 'suppress_rows'`.

- [ ] **Step 3: Implement**

Append to `rebase/sources/energy.py`:

```python
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
    import math

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

    def _missing(value: Any) -> bool:
        return value is None or (isinstance(value, float) and math.isnan(value))

    for row in batch.itertuples(index=False):
        current = lookup.get(row.valid_time)
        if current is None:
            keep.append(True)  # rule 1
            continue
        old_value, old_annotation, old_changed_by = current
        if _missing(row.value):
            if _missing(old_value):
                decision, bucket = False, "null"  # rule 2
            elif on_null is OnNull.KEEP_STORED:
                decision, bucket = False, "null"  # rule 4, keep
            else:
                decision, bucket = True, ""  # rule 4, write
        elif _missing(old_value):
            decision, bucket = True, ""  # rule 3
        elif not skip_unchanged:
            decision, bucket = True, ""  # rules 5-7 disabled
        elif not comparer.values_equal(row.value, old_value):
            decision, bucket = True, ""  # rule 7
        elif row.annotation != old_annotation or row.changed_by != old_changed_by:
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_sources_energy.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rebase/sources/energy.py tests/test_sources_energy.py
git commit -m "Add the seven suppression rules and stored-state selection"
```

---

### Task 5: The store's foundations — keys, parquet IO, registration

**Files:**
- Create: `rebase/sources/energydb.py`
- Create: `tests/test_energydb.py`

**Interfaces:**
- Consumes: `SeriesKey`, `SERIES_CATALOG_COLUMNS`, `RETENTION_TIERS`, `TIMESERIES_TYPES`, `SERIES_VALUES_COLUMNS`, `DataSourceError`, `Frame` from `energy.py`.
- Produces: `EnergyDBStore(bucket, *, prefix="energydb")` with `register_series(...)`, plus module helpers `_months_in_range(start, end) -> list[str]`, `_catalog_key`, `_series_prefix`, `_month_prefix`, `_object_key`, `_encode_parquet(df) -> bytes`, `_decode_parquet(blob) -> Frame`, and `_read_catalog(series_id) -> dict | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_energydb.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_energydb.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'rebase.sources.energydb'`.

- [ ] **Step 3: Implement**

Create `rebase/sources/energydb.py`:

```python
"""Rebase's canonical EnergyDB time-series layout, persisted to bucket storage.

An append-only object log: every write puts a **new immutable** parquet object; nothing is
ever rewritten. Reads list the relevant prefixes, concatenate, and pick winners in pandas
from the definition shared with :func:`rebase.sources.energy.series_values_select`. That
matches EnergyDB itself, where corrections are new rows and never updates — an immutable
object store is the natural substrate for an append-only log.

.. important::
   **This bucket backing is transitional.** It exists so projects can produce, persist and
   read EnergyDB-shaped time series before the toolkit can reach a real EnergyDB. It is
   intended to be replaced by a proper connector in the mould of ``bigquery`` / ``snowflake``
   / ``databricks`` / ``fabric`` — a :class:`~rebase.sources.base.DataSource` with
   ``read``/``read_bitemporal``/``write``, credentials through ``ConnectorSpec``, and the
   store's own ``skip_unchanged`` doing suppression server-side.

   The seam is this module. Everything bucket-specific — key layout, month pruning, parquet
   IO, the client-side read-before-write — lives here and is expected to be discarded.
   Everything meant to survive lives in :mod:`rebase.sources.energy`: the vocabulary
   (``skip_unchanged``, ``unchanged_scope``, :class:`~rebase.sources.energy.Change`,
   :class:`~rebase.sources.energy.OnNull`), the canonical column layout, and the
   winner-selection semantics. Replacing the backing should mean replacing this file.

   Because objects hold exactly ``SERIES_VALUES_COLUMNS`` in order with parquet-preserved
   dtypes, everything written here can be loaded into a real EnergyDB as-is. That shape
   discipline is the point; protect it against any more convenient on-disk format.

pandas and pyarrow are imported lazily inside functions, so this module stays importable
with the toolkit's core (pandas-free) dependencies. Install ``rebase-toolkit[energydb]``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from rebase.sources.base import DataSourceError, Frame
from rebase.sources.energy import (
    RETENTION_TIERS,
    SERIES_CATALOG_COLUMNS,
    TIMESERIES_TYPES,
    SeriesKey,
)

_logger = logging.getLogger("rebase.sources")

_MONTH_FORMAT = "%Y-%m"
_CHANGE_TIME_FORMAT = "%Y%m%dT%H%M%S%f"
_PARQUET_CONTENT_TYPE = "application/vnd.apache.parquet"


def _catalog_key(prefix: str, series_id: int) -> str:
    return f"{prefix}/catalog/{series_id}.json"


def _series_prefix(prefix: str, series_id: int) -> str:
    return f"{prefix}/series/{series_id}/"


def _month_prefix(prefix: str, series_id: int, month: str) -> str:
    return f"{_series_prefix(prefix, series_id)}valid_month={month}/"


def _object_key(prefix: str, series_id: int, month: str, change_time: Any, run_id: int, digest: str) -> str:
    """The object key. Content-addressed by ``digest``, which is what makes it collision-free.

    ``change_time`` and ``run_id`` alone are not enough: ``change_time`` comes from
    ``_resolve_now()``, frozen to the replay bound during a replay, so a replay plus a
    caller-supplied ``run_id`` would produce the same key twice and the second ``put`` would
    silently replace the first — destroying an append-only object. With the digest, identical
    content is idempotent and differing content can never collide.
    """
    stamp = change_time.strftime(_CHANGE_TIME_FORMAT)
    return f"{_month_prefix(prefix, series_id, month)}{stamp}Z-{run_id}-{digest}.parquet"


def _content_digest(blob: bytes) -> str:
    """The first 12 hex chars of sha256 over the encoded object bytes."""
    import hashlib

    return hashlib.sha256(blob).hexdigest()[:12]


def _months_in_range(start: datetime | None, end: datetime | None) -> list[str] | None:
    """The ``valid_month`` partitions spanning ``[start, end)``, or ``None`` when unbounded.

    ``None`` means "no pruning possible" — the caller lists the whole series prefix. Both
    bounds are needed, because one open end could reach any month.
    """
    if start is None or end is None:
        return None
    months: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return months


def _encode_parquet(df: Frame) -> bytes:
    import io

    from rebase._optional import optional_module

    optional_module("pyarrow", "energydb")
    buffer = io.BytesIO()
    df.to_parquet(buffer, index=False)
    return buffer.getvalue()


def _decode_parquet(blob: bytes) -> Frame:
    import io

    import pandas as pd

    from rebase._optional import optional_module

    optional_module("pyarrow", "energydb")
    return pd.read_parquet(io.BytesIO(blob))


class EnergyDBStore:
    """Read and write Rebase's canonical time-series layout in a bucket.

    See the module docstring — this backing is transitional, and a real connector is intended.
    """

    def __init__(self, bucket: Any, *, prefix: str = "energydb") -> None:
        if isinstance(bucket, str):
            from rebase.client import Bucket

            bucket = Bucket.from_name(bucket)
        for method in ("put", "get", "iter_all"):
            if not callable(getattr(bucket, method, None)):
                raise DataSourceError(f"energydb bucket must provide a callable {method}()")
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def register_series(
        self,
        key: SeriesKey,
        *,
        unit: str = "dimensionless",
        timeseries_type: str = "FLAT",
        retention: str = "forever",
        description: str | None = None,
    ) -> SeriesKey:
        """Write the catalog record for ``key`` (idempotent) and return it.

        ``series_id`` is derived from the key, so re-registration overwrites an identical
        record and reads never need a catalog lookup. Registering is how a series becomes
        ``OVERLAPPING``: an unregistered series is treated as ``FLAT``.
        """
        if timeseries_type not in TIMESERIES_TYPES:
            raise DataSourceError(f"timeseries_type must be one of {TIMESERIES_TYPES}; got {timeseries_type!r}")
        if retention not in RETENTION_TIERS:
            raise DataSourceError(f"retention must be one of {RETENTION_TIERS}; got {retention!r}")
        record = {
            "series_id": key.series_id,
            "path": key.path,
            "data_type": key.data_type,
            "name": key.name,
            "canonical_unit": unit,
            "timeseries_type": timeseries_type,
            "retention": retention,
            "description": description or "",
            "inserted_at": None,
        }
        if set(record) != set(SERIES_CATALOG_COLUMNS):
            # A real runtime check rather than an assert: asserts vanish under -O, and this
            # guards the property that makes a future load into a real EnergyDB a load.
            raise DataSourceError(
                f"catalog record does not match SERIES_CATALOG_COLUMNS: {sorted(set(SERIES_CATALOG_COLUMNS) ^ set(record))}"
            )
        payload = json.dumps(record, sort_keys=True).encode("utf-8")
        self.bucket.put(_catalog_key(self.prefix, key.series_id), payload, content_type="application/json")
        return key

    def _read_catalog(self, series_id: int) -> dict[str, Any] | None:
        """The catalog record, or ``None`` when the series was never registered."""
        key = _catalog_key(self.prefix, series_id)
        exists = getattr(self.bucket, "exists", None)
        if callable(exists) and not exists(key):
            return None
        try:
            return json.loads(self.bucket.get(key))
        except Exception:  # noqa: BLE001 - an unreadable catalog means "unregistered", not a failure
            return None

    def _is_overlapping(self, series_id: int) -> bool:
        record = self._read_catalog(series_id)
        return bool(record and record.get("timeseries_type") == "OVERLAPPING")
```

Note `inserted_at` is `None` in the record: the real catalog fills it with `CURRENT_TIMESTAMP()` server-side, and a client-stamped wall-clock value here would be exactly the kind of fiction this branch's earlier work removed from `knowledge_time`. The key is present so the record's shape matches `SERIES_CATALOG_COLUMNS`, which the assertion guards.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_energydb.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rebase/sources/energydb.py tests/test_energydb.py
git commit -m "Add the EnergyDB bucket store foundations and registration"
```

---

### Task 6: `read_series`

**Files:**
- Modify: `rebase/sources/energydb.py` (add to `EnergyDBStore`)
- Test: `tests/test_energydb.py`

**Interfaces:**
- Consumes: `_months_in_range`, `_decode_parquet`, `_series_prefix`, `_month_prefix` (Task 5); `select_series_winners`, `series_keys`, `attach_series_keys` (Tasks 1-2).
- Produces: `EnergyDBStore.read_series(keys, *, start_valid=None, end_valid=None, as_of=None, overlapping=False, include_updates=False) -> Frame`, and `EnergyDBStore._raw_rows(series_ids, months) -> Frame`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_energydb.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_energydb.py -k read_series -q`
Expected: FAIL with `AttributeError: 'EnergyDBStore' object has no attribute 'read_series'`.

- [ ] **Step 3: Implement**

Extend the `energy` import in `rebase/sources/energydb.py` to add `attach_series_keys`, `select_series_winners` and `series_keys`, then add these methods to `EnergyDBStore`:

```python
    def _raw_rows(self, series_ids: list[int], months: list[str] | None) -> Frame:
        """Concatenate every stored object for these series, pruned to ``months`` when known."""
        import pandas as pd

        from rebase.sources.energy import SERIES_VALUES_COLUMNS

        frames: list[Frame] = []
        for series_id in series_ids:
            prefixes = (
                [_month_prefix(self.prefix, series_id, month) for month in months]
                if months is not None
                else [_series_prefix(self.prefix, series_id)]
            )
            for prefix in prefixes:
                for entry in self.bucket.iter_all(prefix=prefix):
                    frames.append(_decode_parquet(self.bucket.get(entry.key)))
        if not frames:
            return pd.DataFrame(columns=list(SERIES_VALUES_COLUMNS))
        return pd.concat(frames, ignore_index=True)

    def read_series(
        self,
        keys: Any,
        *,
        start_valid: datetime | None = None,
        end_valid: datetime | None = None,
        as_of: datetime | None = None,
        overlapping: bool = False,
        include_updates: bool = False,
    ) -> Frame:
        """Point-in-time read with EnergyDB semantics, in the shape a real query returns.

        - default: the latest view — one row per ``valid_time`` (latest issue, latest
          correction within it),
        - ``as_of``: only what was knowable then (bounds ``knowledge_time``); defaults to the
          replay bound during a replay,
        - ``overlapping=True``: every forecast issue (adds ``knowledge_time``),
        - ``include_updates=True``: the full AUDIT trail.

        ``keys`` is one ``(path, data_type, name)`` tuple / :class:`SeriesKey` or a list of
        them. Results carry ``path``/``data_type``/``name`` — never raw ids.
        """
        import pandas as pd

        resolved = series_keys(keys)
        by_id = {key.series_id: key for key in resolved}
        months = _months_in_range(start_valid, end_valid)
        raw = self._raw_rows(list(by_id), months)
        if start_valid is not None and len(raw):
            raw = raw[raw["valid_time"] >= pd.Timestamp(start_valid)]
        if end_valid is not None and len(raw):
            raw = raw[raw["valid_time"] < pd.Timestamp(end_valid)]
        winners = select_series_winners(
            raw, overlapping=overlapping, include_updates=include_updates, as_of=as_of
        )
        return attach_series_keys(winners, by_id)
```

`attach_series_keys` on an empty frame still produces the right columns, because `insert` and `drop` operate on the column axis regardless of row count — which is what makes `test_read_series_empty_returns_the_projection_columns` pass.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_energydb.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rebase/sources/energydb.py tests/test_energydb.py
git commit -m "Add EnergyDBStore.read_series with month pruning"
```

---

### Task 7: `write_series`

**Files:**
- Modify: `rebase/sources/energydb.py` (add to `EnergyDBStore`)
- Test: `tests/test_energydb.py`

**Interfaces:**
- Consumes: `build_values_rows`, `select_current_state`, `suppress_rows`, `OnNull`, `Change`, `SeriesWriteResult` from `energy.py`; `_encode_parquet`, `_object_key`, `_is_overlapping` (Tasks 4-5); `KnowledgeTime` from `base.py`.
- Produces: `EnergyDBStore.write_series(data, key, *, retention="forever", changed_by="", annotation="", run_id=None, knowledge_time=None, skip_unchanged=False, unchanged_scope="auto", change=None, on_null=OnNull.KEEP_STORED) -> SeriesWriteResult`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_energydb.py`:

```python
def _frame(rows):
    """(valid_time, value) pairs -> a SIMPLE input frame."""
    import pandas as pd

    return pd.DataFrame(
        {"valid_time": pd.to_datetime([row[0] for row in rows]), "value": [row[1] for row in rows]}
    )


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
def test_write_series_is_fail_open(monkeypatch) -> None:
    import rebase.sources.energydb as energydb_module

    store, _bucket = _store()
    store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY)

    def _boom(*args, **kwargs):
        raise RuntimeError("comparison exploded")

    monkeypatch.setattr(energydb_module, "suppress_rows", _boom)
    result = store.write_series(_frame([("2026-01-01T00:00Z", 1.0)]), KEY, skip_unchanged=True)
    assert result.fail_open is True
    assert result.rows_written == 1  # the batch was written unfiltered


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_energydb.py -k write_series -q`
Expected: FAIL with `AttributeError: 'EnergyDBStore' object has no attribute 'write_series'`.

- [ ] **Step 3: Implement**

Extend `energydb.py`'s `energy` import with `Change`, `OnNull`, `SeriesWriteResult`, `build_values_rows`, `select_current_state` and `suppress_rows`, then add:

```python
_UNCHANGED_SCOPES = ("auto", "valid_time", "knowledge_time")


    def write_series(
        self,
        data: Any,
        key: SeriesKey,
        *,
        retention: str = "forever",
        changed_by: str = "",
        annotation: str = "",
        run_id: int | None = None,
        knowledge_time: Any | None = None,
        skip_unchanged: bool = False,
        unchanged_scope: str = "auto",
        change: Change | None = None,
        on_null: OnNull = OnNull.KEEP_STORED,
    ) -> SeriesWriteResult:
        """Append a SIMPLE or VERSIONED series, honouring the declared write semantics.

        ``skip_unchanged`` suppresses no-op rewrites; ``change`` declares what "unchanged"
        means (absolute tolerance only, defaulting to exact); ``on_null`` decides whether an
        incoming null may replace a stored value. ``unchanged_scope="auto"`` resolves per
        series from the catalog, so an ``OVERLAPPING`` series bypasses suppression entirely —
        every publication of a forecast is meaningful.

        Suppression is fail-open: if anything in that path raises, the unfiltered batch is
        written and ``fail_open`` is set. Suppression is an optimisation, never a gate.
        """
        if unchanged_scope not in _UNCHANGED_SCOPES:
            raise DataSourceError(f"unchanged_scope must be one of {_UNCHANGED_SCOPES}; got {unchanged_scope!r}")
        if knowledge_time is not None:
            data = knowledge_time.apply(data)
        rows = build_values_rows(
            data, key, retention=retention, changed_by=changed_by, annotation=annotation, run_id=run_id
        )

        if unchanged_scope == "auto":
            overlapping = self._is_overlapping(key.series_id)
        else:
            overlapping = unchanged_scope == "knowledge_time"

        report: dict[str, Any] = {"suppressed_unchanged": 0, "suppressed_null": 0, "sample_valid_times": ()}
        fail_open = False
        # The read is the write path's only added cost, so skip it when nothing could be
        # suppressed anyway.
        must_compare = not overlapping and (skip_unchanged or on_null is OnNull.KEEP_STORED)
        if must_compare:
            try:
                months = sorted({stamp.strftime(_MONTH_FORMAT) for stamp in rows["valid_time"]})
                stored = select_current_state(self._raw_rows([key.series_id], months))
                rows, report = suppress_rows(
                    rows, stored, on_null=on_null, skip_unchanged=skip_unchanged, change=change
                )
            except Exception as exc:  # noqa: BLE001 - fail-open: never block a write
                fail_open = True
                _logger.warning(
                    "energydb: suppression failed for %s/%s/%s, writing the batch unfiltered: %s",
                    key.path,
                    key.data_type,
                    key.name,
                    exc,
                )

        written: list[str] = []
        if len(rows):
            for month, group in rows.groupby(rows["valid_time"].dt.strftime(_MONTH_FORMAT), sort=True):
                # Encode first: the digest must be over the exact bytes being stored.
                blob = _encode_parquet(group)
                object_key = _object_key(
                    self.prefix,
                    key.series_id,
                    str(month),
                    group["change_time"].iloc[0],
                    int(group["run_id"].iloc[0]),
                    _content_digest(blob),
                )
                self.bucket.put(object_key, blob, content_type=_PARQUET_CONTENT_TYPE)
                written.append(object_key)

        suppressed = int(report["suppressed_unchanged"]) + int(report["suppressed_null"])
        if suppressed or fail_open:
            _logger.warning(
                "energydb: wrote %d rows to %s/%s/%s; suppressed %d unchanged and %d null%s",
                len(rows),
                key.path,
                key.data_type,
                key.name,
                report["suppressed_unchanged"],
                report["suppressed_null"],
                " (fail-open)" if fail_open else "",
            )
        return SeriesWriteResult(
            series=key,
            rows_written=int(len(rows)),
            objects_written=tuple(written),
            suppressed_unchanged=int(report["suppressed_unchanged"]),
            suppressed_null=int(report["suppressed_null"]),
            sample_valid_times=tuple(report["sample_valid_times"]),
            fail_open=fail_open,
        )
```

Put `_UNCHANGED_SCOPES` at module level beside the other constants, not inside the class.

The `monkeypatch.setattr(energydb_module, "suppress_rows", ...)` in the fail-open test only works if `suppress_rows` is referenced as a module global — which it is, via the `from … import` at the top of `energydb.py`. Do not switch it to a local import inside the method.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_energydb.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rebase/sources/energydb.py tests/test_energydb.py
git commit -m "Add EnergyDBStore.write_series with declared suppression semantics"
```

---

### Task 8: Factory, exports and packaging

**Files:**
- Modify: `rebase/sources/__init__.py`
- Modify: `pyproject.toml`
- Modify: `pypi/rebase/pyproject.toml`
- Modify: `tests/test_packaging.py:26-37`
- Test: `tests/test_energydb.py`, `tests/test_sources.py`

**Interfaces:**
- Produces: `rb.sources.energydb(*, bucket, prefix="energydb") -> EnergyDBStore`, plus `rb.sources.Change`, `rb.sources.OnNull`, `rb.sources.SeriesWriteResult`, `rb.sources.EnergyDBStore`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_energydb.py`:

```python
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
```

And in `tests/test_packaging.py`, add `"energydb"` to the extras tuple, after `"fabric"`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_energydb.py -k factory tests/test_packaging.py -q`
Expected: FAIL — `AttributeError: module 'rebase.sources' has no attribute 'energydb'`, and the packaging assertion fails on the missing extra.

- [ ] **Step 3: Implement**

In `rebase/sources/__init__.py`, add the factory after `fabric()`:

```python
def energydb(*, bucket: Any, prefix: str = "energydb") -> EnergyDBStore:
    """Create a bucket-backed EnergyDB store. Requires ``rebase-toolkit[energydb]``.

    ``bucket`` is a bucket name or an :class:`rb.Bucket`. This backing is transitional — see
    :mod:`rebase.sources.energydb` for what a real connector would replace.
    """
    from rebase.sources.energydb import EnergyDBStore

    return EnergyDBStore(bucket, prefix=prefix)
```

Extend the imports at the top of that file:

```python
from rebase.sources.energy import Change, OnNull, SeriesKey, SeriesWriteResult
```

and add a `TYPE_CHECKING` import for the annotation, matching the file's import-light intent:

```python
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rebase.sources.energydb import EnergyDBStore
```

For the runtime re-export of `EnergyDBStore`, add a module-level `__getattr__` so importing `rebase` still pulls no pandas:

```python
def __getattr__(name: str) -> Any:
    if name == "EnergyDBStore":
        from rebase.sources.energydb import EnergyDBStore as _EnergyDBStore

        return _EnergyDBStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

Add to `__all__`, alphabetically: `"Change"`, `"EnergyDBStore"`, `"OnNull"`, `"SeriesWriteResult"`, `"energydb"`.

In `pyproject.toml`, add the extra after `fabric`:

```toml
energydb = ["pandas>=2.0", "pyarrow>=14.0"]
```

Add `"rebase-toolkit[energydb]"` to both the `sources` and `all` extras lists.

In `pypi/rebase/pyproject.toml`, add the matching forwarding entry. The existing entries are
multi-line and pinned to the alias package's own `version` (currently `0.7.0` at line 7); match that
exactly, and do **not** bump it:

```toml
energydb = [
    "rebase-toolkit[energydb]==0.7.0",
]
```

`tests/test_packaging.py`'s first test asserts the alias version equals the toolkit version, so a
mismatched pin fails there rather than silently shipping a broken alias.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_energydb.py tests/test_packaging.py tests/test_sources.py -q`
Expected: PASS. `tests/test_sources.py::test_factories_are_exported` must stay green — importing `rebase` still must not import pandas or pyarrow.

- [ ] **Step 5: Verify uv can still resolve the extras**

Run: `uv pip install -e ".[energydb]" -q`
Expected: succeeds. If adding `energydb` to `all` causes a resolution failure, remove it from `all` only, leave it in `sources`, add a one-line comment in `pyproject.toml` recording why, and note it in your report.

- [ ] **Step 6: Commit**

```bash
git add rebase/sources/__init__.py pyproject.toml pypi/rebase/pyproject.toml tests/test_packaging.py tests/test_energydb.py
git commit -m "Export the energydb factory and add its extra"
```

---

### Task 9: Parity test, changelog, and final verification

**Files:**
- Modify: `tests/test_sources_energy.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the parity test**

Append to `tests/test_sources_energy.py`:

```python
# --- SQL / pandas parity --------------------------------------------------------------


def test_sql_builder_still_matches_the_pandas_partitioning() -> None:
    """Guard against the two winner-selection implementations drifting apart.

    ``select_series_winners`` mirrors ``series_values_select``. There is no SQL engine here to
    run the query against, so this asserts the SQL still declares the partitions and ordering
    the pandas implementation reproduces. It is not equivalence — see the spec's stated
    limitation — but a dialect change trips this test instead of drifting silently.
    """
    flat, _params = series_values_select("t", [1])
    assert "PARTITION BY series_id, valid_time " in flat
    assert "ORDER BY knowledge_time DESC, change_time DESC" in flat
    assert "SELECT series_id, valid_time, value" in flat

    overlapping, _params = series_values_select("t", [1], overlapping=True)
    assert "PARTITION BY series_id, valid_time, knowledge_time" in overlapping
    assert "ORDER BY change_time DESC" in overlapping
    assert "SELECT series_id, valid_time, knowledge_time, value" in overlapping

    audit, _params = series_values_select("t", [1], include_updates=True)
    assert "QUALIFY" not in audit
    assert "series_id, valid_time, knowledge_time, change_time, value, changed_by, annotation" in audit


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_pandas_selection_matches_the_documented_semantics_on_a_shared_fixture() -> None:
    """The fixture both implementations are documented to agree on."""
    frame = _values_frame(
        [
            ("2026-01-01T00:00Z", "2026-01-01T06:00Z", "2026-01-01T06:00Z", 1.0),
            ("2026-01-01T00:00Z", "2026-01-01T09:00Z", "2026-01-01T09:00Z", 2.0),
            ("2026-01-01T00:00Z", "2026-01-01T09:00Z", "2026-01-01T10:00Z", 3.0),
            ("2026-01-01T01:00Z", "2026-01-01T09:00Z", "2026-01-01T09:00Z", 4.0),
        ]
    )
    assert list(select_series_winners(frame)["value"]) == [3.0, 4.0]
    assert sorted(select_series_winners(frame, overlapping=True)["value"]) == [1.0, 3.0, 4.0]
    assert len(select_series_winners(frame, include_updates=True)) == 4
```

- [ ] **Step 2: Run the parity tests**

Run: `uv run --no-sync pytest tests/test_sources_energy.py -k "parity or matches_the" -q`
Expected: PASS (these assert current behaviour, so they pass immediately — they are regression guards, not TDD).

- [ ] **Step 3: Add the changelog entry**

Insert as the first bullet under `## Unreleased` → `### Added` in `CHANGELOG.md`, matching the surrounding prose voice:

```markdown
- **`rb.sources.energydb(...)` produces, stores and reads Rebase's canonical EnergyDB
  time-series layout through `rb.Bucket`.** A series is keyed the energydb way —
  `(path, data_type, name)` with a deterministic 63-bit id, so registration is idempotent and
  reads need no catalog lookup — and every write appends a new immutable parquet object under a
  `series_id`/`valid_month` prefix, never rewriting one. Reads pick winners with the same
  semantics a real query would: one row per `(series_id, valid_time)` by latest issue then
  latest correction, `as_of` bounding what was knowable, `overlapping=True` keeping every
  forecast issue, `include_updates=True` returning the full audit trail. Results carry
  `path`/`data_type`/`name` and never a raw id. Because objects hold exactly the canonical
  columns with parquet-preserved dtypes, what lands in the bucket could be loaded straight
  into a real EnergyDB.

  Write semantics are declared rather than implicit. `skip_unchanged=True` makes an
  overlapping refetch window free, with `change=rb.sources.Change.exact()` (the default) or
  `.tolerance(atol)` declaring what "unchanged" means — **absolute only, because a relative
  band silently discards the corrections a refetch exists to capture**. `on_null` defaults to
  `KEEP_STORED`, so a transient upstream gap never overwrites a real stored value, while a null
  with nothing stored still records the gap. Equality compares `value`, `annotation` and
  `changed_by`, with NaN equal to NaN, so a provenance upgrade is a real write. A series
  registered `OVERLAPPING` bypasses suppression entirely, since every publication of a forecast
  is meaningful. Suppression is fail-open — an error writes the batch unfiltered rather than
  blocking it — and always reported: `SeriesWriteResult` carries the counts and sample
  timestamps, so "we saw a revision and declined it" is a number rather than an emergent
  property.

  **This bucket backing is transitional.** It is intended to be replaced by proper EnergyDB
  database support shaped like the other connectors, and the option names mirror
  `energydb`/`timedb` precisely so call sites survive that change. Requires
  `rebase-toolkit[energydb]`.
```

- [ ] **Step 4: Run the full verification sweep**

```bash
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync ruff format rebase/sources/energy.py rebase/sources/energydb.py rebase/sources/bigquery.py rebase/sources/__init__.py tests/test_sources_energy.py tests/test_energydb.py tests/test_packaging.py
uv run --no-sync ruff check .
```

Expected: the four known pre-existing platform failures and nothing more (`4 failed`, with the passed count risen by the tests this plan adds). Ruff clean. If `ruff format` changes anything, re-run `pytest -q` before committing.

- [ ] **Step 5: Confirm no unrelated drift**

```bash
git status --short
git diff --stat master...HEAD
```
Expected: only the files in [File Structure](#file-structure), plus the docs from the spec commits.

- [ ] **Step 6: Commit**

```bash
git add tests/test_sources_energy.py CHANGELOG.md
git commit -m "Add SQL/pandas parity guards and the energydb changelog entry"
```
