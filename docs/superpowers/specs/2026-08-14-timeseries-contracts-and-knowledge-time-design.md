# Time-series contracts and declared knowledge time

Design for GitHub issues [#6](https://github.com/rebase-energy/rebase-toolkit/issues/6) and the
first half of [#7](https://github.com/rebase-energy/rebase-toolkit/issues/7). Surveyed against
`0.7.0`. Branch: `timeseries-contracts-and-energydb`.

## Scope

Two independent pieces, in this order:

1. **Part A — `rb.Index` and `max_null_run`.** All of issue #6. Row-order and spacing constraints
   become declarable in `rb.Contract`, so a missing publication or a dropout is caught by the
   existing validate-before-write pipeline instead of by a post-hoc pass.
2. **Part B — declared knowledge time.** Rules 3 and 4 of issue #7's write semantics:
   `knowledge_time` comes from the source's publication time, and a derived series inherits
   `max(knowledge_time)` of its inputs.

Deferred to a follow-up branch, with reasoning in [Sequencing](#sequencing): `OnNull`, `Change`,
and `rb.sources.energydb()`.

## Sequencing

Issue #7 states that part 2 does not depend on part 1, and that the write-semantics options could
land on the existing warehouse connectors first. That holds for two of its four rules but not the
other two.

Rules 3 and 4 are pure frame transforms. They need no stored state, so they drop onto today's
`DataSource.write` cleanly.

Rules 1 and 2 both require reading what is already stored before deciding whether to write.
`OnNull.KEEP_STORED` has to know whether a real value already sits at a given position, and
`Change.exact()` has to compare against the current winner. The only per-backend write hook today
is `_write(self, df, table, mode)` (`rebase/sources/base.py:227`) — a blind append or replace with
no options bag and no read path. Deciding "is this value unchanged" needs a lookup of the current
winner per `(series_id, valid_time)`, which is the bitemporal layout in `rebase/sources/energy.py`,
not a generic table append.

So rules 1 and 2 do depend on a bitemporal-aware write path. Attempting them before that path
exists means either widening the abstract `_write` signature across four connectors for semantics
none of them can honour, or layering read-before-write into `DataSource.write` against stores that
cannot answer the query. Both are speculative work. They wait.

Part A ships first because it touches `contract.py` alone. The new checks reach the write pipeline
through `validate_frame`, so `sources/base.py` needs no change for it — the two parts do not
conflict.

---

## Part A — row order and spacing constraints

### `Index`

A new plain class in `rebase/contract.py`, following the validating-`__init__` style of `Column`
and `Freshness` rather than a dataclass.

```python
class Index:
    def __init__(
        self,
        column: str,
        *,
        monotonic: bool = False,
        max_gap: str | int | float | timedelta | Duration | None = None,
    ) -> None
```

Constructor validation:

- `column` must be a non-empty string; it is stripped, as `Column.name` is.
- At least one of `monotonic` or `max_gap` must be set. An `Index` declaring no constraint is
  rejected rather than silently compiling to zero checks.
- `max_gap` is coerced through `Duration.coerce(value, field_name="max_gap")` and must be
  positive. An `int` or `float` means **seconds**, matching what `Freshness` does with a bare
  number. Calendar durations — any `Duration` carrying `months` — are rejected: the check compares
  a fixed `timedelta` diff, and a month-long bound has no fixed length.

`monotonic=True` means **strictly increasing**. Duplicate index values violate it. That overlaps
`primary_key` harmlessly: a contract declaring both reports two failures for the same duplicate,
which is accurate rather than confusing.

Gaps are measured between **consecutive rows as delivered**, not after sorting. Monotonicity is a
separate assertion, and a frame that fails it should not have its gap check quietly repaired by a
sort.

Serialisation:

```python
{"column": "valid_time", "monotonic": True, "max_gap": "PT1H"}
```

`monotonic` and `max_gap` are omitted when unset, matching how `Contract.to_dict` omits
`min_rows` and `watermark_column`. `max_gap` is stored as `Duration.isoformat()`, so both
`"PT1H"` and `"1h"` on input store as `"PT1H"`.

### `Column.max_null_run`

`Column` gains one keyword:

```python
max_null_run: int | None = None
```

Validated as a positive int with an explicit `bool` rejection, the same guard `Contract.min_rows`
uses at `rebase/contract.py:179`.

A null run is a per-column property that happens to depend on row order, and `Column` already
owns the per-column constraints (`not_null`, `between`, `isin`). Putting it there needs no
`columns=` list to cross-validate against declared columns, and it allows different thresholds per
column — a primary feed tolerating three missing periods while a backup tolerates twelve.

Stored as `x-max-null-run` on the column property, alongside the existing `x-not-null`.

### `Contract.index`

`Contract` gains `index: Index | None = None`, validated the way `watermark_column` already is:

- `index.column` must name a declared column.
- If `max_gap` is set, the index column's dtype must be `timestamp` or `date`. A time-based bound
  against a `string` or `bool` column is meaningless.
- If `monotonic` is set, the index column's dtype must be one of `timestamp`, `date`, `int` or
  `float`. `string` and `bool` are rejected: pandas will happily order them, but an index that is
  meaningfully "increasing" is a temporal or numeric one, and accepting the others invites a
  contract that passes for reasons its author did not intend.
- Any column declaring `max_null_run` requires the contract to declare an `index`. Without one,
  "consecutive" has no defined meaning — row order is whatever the frame happened to arrive in.

Serialised into the existing extension block as `x-rebase["index"]`. Stored contracts stay JSON
Schema plus extension; no new storage format.

### Example

```python
rb.Contract(
    columns=[
        rb.Column("valid_time", "timestamp", not_null=True),
        rb.Column("value", "float", between=(0, 40_000), max_null_run=3),
        rb.Column("backup", "float", max_null_run=12),
    ],
    primary_key=("valid_time",),
    index=rb.Index(column="valid_time", monotonic=True, max_gap="PT1H"),
)
```

### The three checks

`compile_checks` grows from eight checks to eleven. Each factory returns the same closure contract
the existing eight use — `run(df) -> CheckFailure | None`, `None` meaning pass.

| check | column | detail line |
| :-- | :-- | :-- |
| `index_monotonic` | the index column | `valid_time is not strictly increasing` |
| `index_max_gap` | the index column | `gap of 6:00:00 exceeds PT1H` |
| `null_run` | the declaring column | `run of 5 consecutive nulls exceeds 3` |

Shared semantics, inherited from the existing factories:

- **Absent column returns `None`.** Only `missing_column` reports the root cause, as at
  `rebase/contract.py:438`, `450`, `462` and `512`.
- **`sample_rows` are positional**, via `_sample_positions`, capped at `MAX_SAMPLE_ROWS`.
- **`count` is the number of violating rows**, not the number of runs or gaps, so the
  `violation_message` header arithmetic stays honest.
- **The body is wrapped in `except Exception: return None`**, as `range` and `isin` are at
  `rebase/contract.py:484` and `503`, so a dtype mismatch surfaces only as a `dtype` failure
  rather than as three cascading ones.

Check-specific notes:

- `index_monotonic` reports the positions where the diff is non-positive — the offending row, not
  its predecessor.
- `index_max_gap` compares `series.diff()` against the coerced `timedelta`. The first row has a
  null diff and is never a violation. The detail names the widest observed gap.
- `null_run` computes run lengths over the null mask and flags every row belonging to a run longer
  than the threshold. All rows in an offending run are reported, since a run's extent is the
  useful information.

Checks are appended in `compile_checks` so that `null_run` sits with the per-column checks and the
two index checks sit with the table-level ones, after `primary_key`. Order affects only report
readability.

### Exports

`Index` joins the five names `rebase/__init__.py:47-53` re-exports from `rebase.contract`, and its
`__all__`. `CheckFailure`, `compile_checks` and `validate_frame` remain unexported, reached
directly from `rebase.contract` as the tests already do.

### `Freshness` and the second duration grammar

`Freshness.max_age` validates against its own inline regex, `^\d+\s*(s|m|h|d)?$`, at
`rebase/contract.py:233`. It does not use the `Duration` grammar in `rebase/timing.py`. So
`Duration.parse("PT1H")` succeeds while `Freshness("PT1H")` is rejected — and once `max_gap="PT1H"`
works, a contract can hold two duration fields that accept different grammars.

`Freshness` will delegate to `Duration.coerce`, keeping its existing shortcuts as a pre-pass:

- `timedelta`, `int` and `float` continue to convert directly to seconds.
- A bare-digit string continues to mean seconds, which `Duration.parse` alone rejects.
- Anything else goes to `Duration.coerce`, so ISO-8601 and compact forms both work.
- Calendar durations are rejected, as for `max_gap`.

**The stored representation must not change for any input accepted today.** `config_diff` compares
in-code contracts against stored ones, and `preflight_datasets` blocks `deploy` on drift. If
`Freshness("5m")` started serialising to anything other than `{"max_age": "5m"}`, every dataset
using it would report phantom drift without anyone having touched it. So today's normalisation is
preserved exactly, and only newly accepted ISO inputs need a representation — those normalise to
`"{n}s"`.

This is a strict superset: every string valid today stays valid and serialises identically.

### Deliberately out of scope

- **A `finite=` constraint on `Column`.** Issue #6 notes that `_make_dtype_check` compares the
  pandas dtype only, so `inf` passes a `float` column. True, but a `between` bound already catches
  infinities — `inf > maximum` is `True` — so a value-must-be-finite check falls out of the
  existing vocabulary. There is no gap to close.
- **Bounds derived from a series' own history.** Out of scope per the issue itself. They need
  history the validator does not have.
- **A README section.** `README.md` documents none of datasets, contracts or sources today.
  Writing that section is its own docs task, not a rider on this branch.

### Tests

`tests/test_contract.py`, under a new `# --- index constraints ---` banner, following the file's
conventions: parametrized `("kwargs", "match")` tuples with `pytest.raises(..., match=...)` for
constructor validation, whole-dict equality against a literal expected shape for serialisation,
and `@pandas_only` engine tests built on the existing `_frame` and `_failure` helpers.

Coverage:

- Constructor validation: inert `Index`, unknown index column, `max_gap` on a non-temporal column,
  `monotonic` on a `string` column, calendar `max_gap`, non-positive `max_gap`, `max_null_run` as
  `bool`, `max_null_run` without a declared `index`.
- Round-trip: `to_dict` shape, and `from_dict(to_dict(...))` equality.
- Engine: a clean hourly frame passing all three; one duplicate and one out-of-order row failing
  `index_monotonic`; a six-hour hole failing `index_max_gap` with the widest gap in the detail; a
  five-null run failing `null_run` at threshold three while a three-null run passes; per-column
  thresholds diverging on one frame; absent index column reporting `missing_column` only.
- `Freshness`: every currently accepted input serialising byte-identically to today, plus
  `Freshness("PT1H")` now accepted.

---

## Part B — declared knowledge time

### `KnowledgeTime`

A new class in `rebase/sources/base.py`, next to `BitemporalSpec` as its write-side counterpart.

```python
KnowledgeTime.from_source("issued_at")       # rule 3
KnowledgeTime.from_inputs(actuals, weather)  # rule 4
KnowledgeTime.at(moment)                     # explicit escape hatch
```

Spelled `rb.sources.KnowledgeTime`, not `rb.KnowledgeTime` as issue #7 sketches. No source symbol
is hoisted to the `rb.*` top level today — `BitemporalSpec` and `SeriesKey` are both
`rb.sources.*` — and matching that convention beats making one name special. Hoisting later is a
one-line change if the ergonomics grate.

There is deliberately **no** `KnowledgeTime.now()`. Wall-clock stamping is the failure mode the
issue is about, and omitting the argument already gives today's behaviour unchanged, so a public
constructor for it would only lend it legitimacy.

Resolution is strict, because every failure mode here is otherwise silent:

- **`from_source(column)`** copies the named column to `knowledge_time`, coerced to UTC. Raises
  `DataSourceError` if the column is missing, and also if it carries nulls — a null publication
  time is not a knowledge time, and letting it through would reintroduce a wall-clock fallback
  under another name. A timezone-naive column warns before being assumed UTC, matching `at()`
  and `energy.py`'s `_utc` helper: if the true zone is not UTC, every knowledge time is silently
  wrong by the offset, which is exactly the class of failure this class exists to make loud.
- **`from_inputs(*frames)`** takes the maximum over each frame's `knowledge_time` column and
  assigns that scalar to every row. Raises `DataSourceError` naming the offending input by
  position if a frame has no such column. This composes with `read_bitemporal`, whose output
  already carries `knowledge_time` via `apply_bitemporal`. Requires at least one frame.
- **`at(moment)`** takes an explicit `datetime`. A naive value warns and is assumed UTC, matching
  the `"timezone-naive; assuming UTC"` convention at `rebase/sources/energy.py:99`.

### Pipeline placement

`DataSource.write` gains `knowledge_time: KnowledgeTime | None = None`. The stamp is applied
before validation, since `knowledge_time` becomes a column the contract can constrain:

```
apply knowledge_time → resolve contract → validate → write → signal
```

It is applied ahead of the `dataset=`-absent early return at `rebase/sources/base.py:270`, because
provenance is independent of contracts and a frame written without a dataset still deserves a
correct knowledge axis.

The frame is copied before stamping, so `write()` never mutates the caller's dataframe. The copy
happens only when `knowledge_time` is passed; the existing no-argument path is untouched.

`_derive_watermark` then runs against the stamped frame, which is correct — the watermark is
derived from what was written.

### Replay interaction

A declared `knowledge_time` is data, so `_resolve_now()` must not override it. Replay does not
touch a resolved value.

But if a replay resolves a knowledge time *later* than `REBASE_REPLAY_KNOWLEDGE_TIME`, that is a
leak: the replay is writing rows the original run could not have known. This logs a warning to the
`rebase.sources` logger rather than raising, consistent with the other replay guards there. The
frame is still written — a replay writing slightly-too-fresh derived data is a diagnostic, not a
reason to fail a job mid-pipeline.

### The wall-clock bug in `build_values_rows`

`rebase/sources/energy.py:173` computes `now = pd.Timestamp(datetime.now(UTC)).as_unit("us")` and
uses it for both the `knowledge_time` fallback and `change_time`. It bypasses the replay-aware
`_resolve_now()` that `apply_bitemporal` uses, so the toolkit's own canonical-layout helper has
exactly the failure mode issue #7 describes.

Two changes:

1. Route through `_resolve_now()`, so a replay stamps the replay bound rather than wall-clock.
2. Accept an already-resolved `knowledge_time`, so a declared option flows through instead of being
   overwritten by the fallback.

`change_time` keeps using `_resolve_now()`. "When we wrote it" is genuinely what that column
means, so wall-clock is right there — and under replay, the replay bound is the correct answer for
the same reason.

The only behavioural difference for existing callers is under replay, where the current value is
wrong.

### Out of scope for this branch

`OnNull`, `Change`, the `series=` kwarg on `write()`, and `rb.sources.energydb()` itself. See
[Sequencing](#sequencing).

### Tests

`tests/test_sources.py`, following its conventions — hand-written `DataSource` subclasses rather
than mocks, duck-typed dataset doubles, `monkeypatch.setenv` for replay bounds, and asserted
warnings and log records.

A recording `_FakeSource` variant captures the frame reaching `_write`, which is what makes the
stamp observable.

Coverage:

- `from_source` stamping the column's values; raising on a missing column; raising on nulls.
- `from_inputs` taking the max across frames; raising on a frame with no `knowledge_time`, naming
  its position; raising on no frames.
- `at` stamping a scalar; warning on a naive datetime.
- The caller's frame is not mutated.
- The stamp is visible to validation — a contract declaring `knowledge_time` `not_null` passes
  because of the stamp, not despite it.
- The stamp applies on the no-`dataset=` path.
- A replay resolving past the replay bound logs to the `rebase.sources` logger and still writes.

`tests/test_sources_energy.py` for the `build_values_rows` fix: under
`REBASE_REPLAY_KNOWLEDGE_TIME`, both `knowledge_time` fallback and `change_time` take the replay
bound; a passed-in `knowledge_time` survives instead of being overwritten.

---

## Verification

Per `ONBOARDING.md` §7:

- `uv run --no-sync ruff check .` clean for touched files.
- `ruff format` on touched files only — a bare `ruff format .` reformats two files already
  unformatted on `master`.
- `uv run --no-sync pytest -q` — expect exactly the one known pre-existing failure,
  `tests/test_client.py::test_project_deploy_registers_step_workflow_graph`. Anything more is ours.
- `ty check` is not a clean gate (~81 pre-existing diagnostics); judge by added diagnostics only.
- `CHANGELOG.md` gains an entry under `## Unreleased` → `### Added`, hand-written in the existing
  prose style. No version bump — that is a separate release step touching three files.
