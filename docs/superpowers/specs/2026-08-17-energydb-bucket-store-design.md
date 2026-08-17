# The EnergyDB bucket store

Design for the remainder of GitHub issue
[#7](https://github.com/rebase-energy/rebase-toolkit/issues/7) — its Part 1, and rules 1 and 2 of
its Part 2. Together with the work already on this branch (rules 3 and 4), it closes #7. Surveyed
against `0.7.0`. Branch: `timeseries-contracts-and-energydb`, PR
[#8](https://github.com/rebase-energy/rebase-toolkit/pull/8).

## What this is, and what the issue implies instead

Issue #7 reads as a request for a live database connector. It says *"the connector, so the uniform
`read` / `read_bitemporal` / `write` surface covers our own store"*, sketches
`rb.sources.energydb(connection="prod")`, and frames Part 1 as *"the house format is the one you
can't read or write through a connector."*

That is not the intent. The intent, clarified directly:

- **In scope** — support for the EnergyDB *data model*: producing time-series data in the exact
  canonical shape, persisting it, and reading it back in the shape a real EnergyDB query would
  return.
- **Persistence** — `rb.Bucket`, which the toolkit already has.
- **The binding requirement** — what lands in the bucket must be shaped so it *could* be loaded
  straight into a real EnergyDB later, with no transformation.
- **Out of scope** — talking to an EnergyDB instance. No ClickHouse driver, no connection
  credentials, no new REST endpoints. That is future work for the toolkit.

Two facts make the out-of-scope half impossible here anyway, and are worth recording so nobody
re-derives them:

- **There is no ClickHouse client in this repo.** `clickhouse-connect` and `clickhouse-driver`
  appear nowhere — not in `pyproject.toml`, not in any extra, not in code. Every mention of
  ClickHouse is docstring prose (`energy.py:6`, `energy.py:43`, `bigquery.py:36`). The only
  concrete implementation of the canonical layout is the BigQuery DDL at `bigquery.py:38-68`.
- **There is no platform-mediated path.** Grepping all of `client.py` for
  `series|timedb|timeseries|energydb` returns zero matches. There is no `/series`, `/timeseries`,
  `/data` or `/query` endpoint; the dataset methods are signal-channel only.

## Architecture

An append-only object log with winner selection at read time.

Every write puts a **new immutable** parquet object; nothing is ever rewritten. A read lists the
relevant prefixes, fetches the matching objects, concatenates them, and applies winner selection in
pandas.

This is a fit rather than a workaround: EnergyDB is append-only and corrections are *new rows,
never updates*, and an immutable object store is the natural substrate for an append-only log. The
two models agree. `include_updates=True` — the AUDIT shape — then costs nothing: skip the dedup
step and the raw log *is* the answer.

Parquet, not CSV, because it round-trips `datetime64[us, UTC]`, `int64` and `float64` exactly. That
dtype fidelity is what makes "could be loaded straight into EnergyDB" true rather than hopeful.

Two approaches were rejected. **Rewriting one snapshot object per series** gives single-`get` reads
but rewrites history on every write against an append-only model, loses rows under concurrent
writers, and scales with total series length rather than batch size. **Querying parquet in place
via `Bucket.uri`** with duckdb or `pandas.read_parquet` would give the best shape fidelity and need
almost no new selection logic, but it requires the caller to hold cloud credentials for that URI,
and `Bucket`'s entire design is short-lived signed capability URLs *so hosted code never receives
cloud credentials* (`client.py:995-1002`).

## Surface

Not a `DataSource` subclass. `DataSource`'s abstract contract is
`_read_frame(query: str, params)` and its `read()` normalises *driver* errors; a bucket has no query
language, so subclassing would mean inheriting an interface the class must refuse.

```python
store = rb.sources.energydb(bucket="forecasts")     # a name, or an rb.Bucket instance
key   = rb.sources.SeriesKey("portfolio/site-1/t01", "forecast", "electricity.supply")

store.register_series(key, unit="MW", timeseries_type="FLAT", retention="forever")

result = store.write_series(
    df, key,
    knowledge_time=rb.sources.KnowledgeTime.from_source("issued_at"),
    skip_unchanged=True,
    change=rb.sources.Change.exact(),
    on_null=rb.sources.OnNull.KEEP_STORED,
)

back = store.read_series(key, start_valid=t0, end_valid=t1, as_of=t2)
```

Signatures:

```python
def energydb(*, bucket: str | Bucket, prefix: str = "energydb") -> EnergyDBStore

class EnergyDBStore:
    def register_series(
        self,
        key: SeriesKey,
        *,
        unit: str = "dimensionless",
        timeseries_type: str = "FLAT",
        retention: str = "forever",
        description: str | None = None,
    ) -> SeriesKey

    def write_series(
        self,
        data: Any,
        key: SeriesKey,
        *,
        retention: str = "forever",
        changed_by: str = "",
        annotation: str = "",
        run_id: int | None = None,
        knowledge_time: KnowledgeTime | None = None,
        skip_unchanged: bool = False,
        unchanged_scope: str = "auto",
        change: Change | None = None,
        on_null: OnNull = OnNull.KEEP_STORED,
    ) -> SeriesWriteResult

    def read_series(
        self,
        keys: Any,
        *,
        start_valid: datetime | None = None,
        end_valid: datetime | None = None,
        as_of: datetime | None = None,
        overlapping: bool = False,
        include_updates: bool = False,
    ) -> Frame
```

`keys` accepts a `SeriesKey`, a `(path, data_type, name)` tuple, or a list of either — matching
`bigquery.py:242`'s `_series_keys`, which this design hoists into `energy.py`.

`register_series` validates `timeseries_type` against `TIMESERIES_TYPES` and `retention` against
`RETENTION_TIERS`. Both constants exist today; `TIMESERIES_TYPES` is currently dead code, never
referenced anywhere, and `bigquery.register_series` accepts an unvalidated string. This design
starts validating it, because the store's suppression behaviour now *depends* on it.

## Module layout

`energy.py` is 268 lines and should not absorb bucket IO as well.

| File | Responsibility |
| :-- | :-- |
| `rebase/sources/energy.py` | The dialect-neutral canonical-layout layer. Gains `select_series_winners`, the `OnNull` / `Change` declarations, `SeriesWriteResult`, and the suppression rules. Absorbs `_series_keys` and the id→key remap from `bigquery.py`. |
| `rebase/sources/energydb.py` | **New.** The bucket-backed store: object layout, prefix listing and month pruning, parquet IO, read-before-write orchestration. |
| `rebase/sources/__init__.py` | The `energydb` factory plus the new exports. |

Declaring `OnNull` / `Change` in `energy.py` but *applying* them from `energydb.py` is deliberate:
the declarations are properties of the layout, so a future real connector reuses the same
vocabulary instead of inventing a second one. That is the centralisation the issue asks for.

Two helpers currently trapped in `bigquery.py` move to `energy.py` because both backings need them:
`_series_keys` (`bigquery.py:242`) and the `series_id`→`(path, data_type, name)` remap block
(`bigquery.py:231-234`). `bigquery.py` then imports them, so behaviour there is unchanged.

## Storage layout

```
{prefix}/catalog/{series_id}.json
{prefix}/series/{series_id}/valid_month=2026-08/{change_time}-{run_id}.parquet
```

- `series_id` is the deterministic 63-bit `sha256(path\x1fdata_type\x1fname)` id. Because it is
  derived rather than assigned, `register_series` is an idempotent put and reads never need a
  catalog lookup.
- The `valid_month` partition deliberately mirrors BigQuery's
  `PARTITION BY TIMESTAMP_TRUNC(valid_time, MONTH)` (`bigquery.py:38-53`), so the two backings prune
  alike. A batch spanning months writes one object per month, which is what keeps pruning
  meaningful.
- `change_time` is rendered in compact basic ISO form (`20260817T101500123456Z`) so keys sort
  lexicographically in `Bucket.list()` order, and `run_id` disambiguates two writes in the same
  microsecond. Together they make writes collision-free without any coordination.
- Each parquet object holds exactly `SERIES_VALUES_COLUMNS`, in that order, as
  `build_values_rows` already returns.
- The catalog object holds one `SERIES_CATALOG_COLUMNS` record as JSON. JSON rather than parquet
  because it is a single row, is read on the write path, and benefits from being human-readable in
  a bucket listing.

## Write semantics

### The declarations

```python
class OnNull(Enum):
    KEEP_STORED = "keep_stored"   # default: never replace a stored real value with a null
    WRITE_NULL  = "write_null"    # a null is a real observation; record the gap

class Change:
    @classmethod
    def exact(cls) -> Change            # default

    @classmethod
    def tolerance(cls, atol: float) -> Change   # ABSOLUTE only; atol must be > 0
```

`Change.exact()` is the default and `tolerance()` must be opted into, because over-suppression was
the observed failure. `tolerance()` takes an **absolute** bound and there is deliberately no
relative variant — see [Provenance](#provenance-of-these-decisions). If someone needs a relative
band later they can ask with a concrete case; the toolkit will not offer one by default.

`atol <= 0` is rejected: it is either a no-op (`0`) or nonsense (negative), and both are better
spelled `exact()`.

`change=None` — the parameter default — means `Change.exact()`. The parameter is typed
`Change | None` rather than defaulting to `Change.exact()` directly so the default is not a mutable
module-level singleton, but the two are equivalent in behaviour.

`change` is consulted **only when `skip_unchanged=True`**. With `skip_unchanged=False` the
value-comparison rules do not run at all, so passing a `change` has no effect; that combination is
allowed rather than an error, because it is the natural state of a caller who has set `change` once
and toggles `skip_unchanged` per call.

### Equality

Two rows are "unchanged" when their **`value`, `annotation` and `changed_by` all match**, with
`NaN` treated as equal to `NaN`. This mirrors timedb's native exact comparison
(`timedb/write.py:228`: *"`value == value_st` (NaN==NaN also equal; annotation + changed_by must
also match)"*).

The `NaN == NaN` clause matters. The live application implementation returns `False` for any NaN
comparison and therefore rewrites every null row on every pass; matching timedb means the toolkit
does not inherit that.

`annotation` and `changed_by` participating in equality is what makes a provenance upgrade — the
same numeric value arriving with a better annotation — a genuine write rather than a suppressed
no-op.

### The rules

Seven rules, evaluated per row against the stored winner:

| # | stored | incoming | action |
| :-: | :-- | :-- | :-- |
| 1 | absent | anything | **write** — a new point |
| 2 | null | null | skip — nothing to say |
| 3 | null | real | **write** — the gap fill, backfill's whole purpose |
| 4 | real | null | `OnNull.KEEP_STORED` → skip; `WRITE_NULL` → write |
| 5 | equal value, equal annotation, equal `changed_by` | — | skip — genuinely unchanged |
| 6 | equal value, differing annotation or `changed_by` | — | **write** — still meaningful |
| 7 | differing value | — | **write** — the revision |

**Rules 1–4 always apply. Rules 5–7 apply only when `skip_unchanged=True`**; with it `False`, any
row that survives rules 1–4 is written without value comparison. So the two flags are independent
axes: `on_null` protects stored values from nulls, `skip_unchanged` suppresses no-op rewrites, and
either can be used without the other.

These seven generically express the nine cases recorded in the application's own carry-over rules.
Two of those nine — *"equal, `a03_forward_fill` superseded by a real observation → write"* and
*"equal, different annotation → write"* — are both instances of rule 6, so the toolkit expresses
the general rule and does **not** hardcode `a03_forward_fill` or any other application-specific
annotation vocabulary.

Rule 4 is the subtle one and rule 3 explains why: rule 1 of the issue forbids *replacing* a real
value with a missing one. When nothing is stored there is no real value to protect, so writing the
null is correct — it records that the upstream reported a gap.

### OVERLAPPING series bypass suppression entirely

For a series registered `OVERLAPPING`, no suppression is applied, regardless of `skip_unchanged`,
`change` or `on_null`.

Every publication of a forecast is meaningful — that is the whole reason the series is `OVERLAPPING`
rather than `FLAT`. A republication at a new `knowledge_time` whose value happens to match the
previous one is a genuine new observation, and suppressing it loses information no later read can
recover.

`unchanged_scope` accepts `"auto"` (the default), `"valid_time"` or `"knowledge_time"`. `"auto"`
resolves per series from the catalog: `OVERLAPPING` → bypass, `FLAT` → compare on
`(series_id, valid_time)`. An unregistered series defaults to `FLAT`, so registering first is how a
series gets `OVERLAPPING` treatment — stated plainly because it would otherwise surprise someone.

This is a **deliberate divergence** from today's upstream behaviour, documented as such in
[Divergences](#deliberate-divergences-from-todays-upstream).

### Failure behaviour: fail-open

If anything in the suppression path raises — a read failure, a dtype surprise, a comparison error —
the **unfiltered batch is written** and `SeriesWriteResult.fail_open` is set to `True`, with a
warning on the `rebase.sources` logger.

Suppression is an optimisation, never a gate. A bug in it must not lose data. Writing a duplicate
row is recoverable; declining to write a correction is not.

### Suppression is always reported

```python
@dataclass(frozen=True)
class SeriesWriteResult:
    series: SeriesKey
    rows_written: int
    objects_written: tuple[str, ...]
    suppressed_unchanged: int
    suppressed_null: int
    sample_valid_times: tuple[str, ...]   # ISO strings, capped at MAX_SAMPLE_VALID_TIMES = 10
    fail_open: bool = False
```

`suppressed_unchanged` counts rows dropped by rules 5–7's skip branch; `suppressed_null` counts rows
dropped by rules 2 and 4. `sample_valid_times` samples the `valid_time` of **suppressed** rows —
drawn from both categories, capped in total — so a reader can go look at specific points rather than
only a count. The cap mirrors `CheckFailure.sample_rows`' existing `MAX_SAMPLE_ROWS = 10`
convention in `contract.py`.

`knowledge_time` precedence is inherited from `build_values_rows` unchanged: a `knowledge_time`
column already in the frame (the VERSIONED shape, per-row) wins over the `knowledge_time=`
declaration, which wins over the replay-aware batch clock.

Plus a summary line on the `rebase.sources` logger whenever anything was suppressed or the path
failed open.

This is the issue's central complaint: an undeclared, unreported threshold made a real problem
invisible until someone went looking. The application's own record is that a quality checker was
widened to the same band, leaving points that could never heal — measured at 14 of 111 on one
series. A returned count and a log line make that a number instead of an emergent property.

### The read-before-write, and when it is skipped

1. Build canonical rows via `build_values_rows` — already replay-aware after this branch's earlier
   work.
2. Compute the batch's `valid_time` range and the months it spans.
3. Read the current winners for that series over just those months.
4. Left-join on the comparison key and apply the seven rules.
5. Write what survives, one object per month.

The read is **skipped entirely when no suppression can occur** — that is, when `skip_unchanged` is
`False` and `on_null` is `WRITE_NULL`. Worth stating because the read is the write path's only
added cost, and a caller who wants raw append speed can get it.

## Read path

`read_series` reproduces the projection a real query returns, matching `bigquery.read_series`'s
remap exactly: `series_id` is never exposed, and `path` / `data_type` / `name` are inserted at the
front.

| mode | columns |
| :-- | :-- |
| default (FLAT) | `path, data_type, name, valid_time, value` |
| `overlapping=True` | `path, data_type, name, valid_time, knowledge_time, value` |
| `include_updates=True` | `path, data_type, name, valid_time, knowledge_time, change_time, value, changed_by, annotation` |

Winner selection is defined **once**, as a pandas function in `energy.py`:

```python
def select_series_winners(
    df: Frame,
    *,
    overlapping: bool = False,
    include_updates: bool = False,
    as_of: datetime | None = None,
) -> Frame
```

Semantics, mirroring `series_values_select` (`energy.py:210-268`) clause for clause:

- `as_of` defaults to `_replay_knowledge_time()`, so a replay reads point-in-time automatically.
  When set, rows with `knowledge_time > as_of` are dropped.
- `include_updates=True` returns every row, no dedup, ordered
  `series_id, valid_time, knowledge_time, change_time`.
- Otherwise one row survives per partition — `(series_id, valid_time, knowledge_time)` when
  `overlapping`, else `(series_id, valid_time)` — chosen by `change_time DESC` when `overlapping`,
  else `knowledge_time DESC, change_time DESC`. Final order `series_id, valid_time`.

`start_valid` / `end_valid` are half-open (`>= start`, `< end`), matching the SQL.

Month pruning derives the candidate `valid_month` partitions from `start_valid`/`end_valid` and
lists only those prefixes. With neither bound, the whole series prefix is listed.

## Packaging

A new `energydb` extra: `["pandas>=2.0", "pyarrow>=14.0"]`. `pyarrow` is currently only a
transitive pin of the `snowflake` and `databricks` extras and is never imported by name anywhere in
`rebase/`; parquet IO makes it a direct requirement.

Four files must change in lockstep, or `tests/test_packaging.py` fails:

1. `pyproject.toml` — the new extra, plus `energydb` added to the `sources` and `all` umbrellas.
2. `pypi/rebase/pyproject.toml` — the alias package must forward the same extra.
3. `tests/test_packaging.py:26-37` — the extras tuple is an **exact-equality** assertion.
4. `[tool.uv] conflicts` — reviewed, and no new entry is expected: `energydb` pulls only
   `pandas>=2.0` and `pyarrow`, neither of which conflicts with the existing forks. If adding it to
   `all` surfaces a resolution failure, the fallback is to leave `energydb` out of `all` only, and
   record why.

## Testing

`Bucket` is signed-URL and HTTP based, so tests use a hand-written in-memory `_FakeBucket`
implementing the subset used (`put`, `get`, `list`, `iter_all`, `exists`, `delete`). The repo's
convention is hand-written fakes and duck-typed doubles, never `unittest.mock`.

The fake **records which keys were fetched**, which is what allows asserting that pruning actually
prunes rather than merely returning the right rows.

Coverage:

- **Round-trip fidelity** — write then read returns identical values, and dtypes survive parquet
  (`datetime64[us, UTC]`, `int64`, `float64`).
- **Winner selection** — a correction supersedes the original; `as_of` gives point-in-time;
  `overlapping=True` keeps both knowledge times; `include_updates=True` returns every row.
- **Projections** — each of the three column layouts above, exactly, including that `series_id`
  never appears.
- **Pruning** — a batch spanning a month boundary writes two objects; a single-month read fetches
  only one (asserted against the fake's fetch log).
- **The seven rules** — as a parametrised table, one case per rule, plus `OnNull.WRITE_NULL`
  flipping rule 4.
- **Equality** — `NaN == NaN` suppresses; a differing `annotation` writes; a differing `changed_by`
  writes.
- **`Change`** — `exact()` suppresses identical; `tolerance(atol)` suppresses within the band; a
  correction just outside the band **is** written; `atol <= 0` is rejected. And specifically: a
  0.5-unit correction on a 5,000-magnitude series **is** written under `tolerance(1e-6)` — the
  direct regression test for the production defect.
- **OVERLAPPING bypass** — a registered `OVERLAPPING` series writes an identical republication at a
  new `knowledge_time` even with `skip_unchanged=True`; the equivalent `FLAT` series suppresses it.
- **Fail-open** — a deliberately broken comparison writes the full batch and sets `fail_open`.
- **Reporting** — counts are correct, samples are capped at 10, and the log line is emitted.
- **Replay** — `as_of` defaults to the replay bound under `REBASE_REPLAY_KNOWLEDGE_TIME`.
- **Skipped read** — with `skip_unchanged=False` and `on_null=WRITE_NULL`, no `get` is issued.
- **Registration** — idempotent; invalid `timeseries_type` and `retention` are rejected.
- **Parity** — `select_series_winners` against a shared fixture, plus an assertion that
  `series_values_select` still emits the same `PARTITION BY` / `ORDER BY` clauses, so a future
  dialect change trips a test instead of drifting silently.

### An honest limitation

Winner-selection parity between the pandas reader and the SQL builder is established by **shared
documented semantics plus a hand-verified fixture** — not by differential execution. There is no
BigQuery or ClickHouse engine available here to run the SQL against. The two could therefore drift
in a way the tests do not catch. The parity test asserting the SQL's clauses is a partial
mitigation; it is not equivalence.

## Provenance of these decisions

The write semantics are not invented here. They are taken from the application's own recorded
decisions, which are more advanced than issue #7 conveys:

- **`Change.exact()` as default, `.tolerance(1e-6)` as an option, declared and reported** — the
  toolkit-level resolution in `docs/rebuild/toolkit-request-energydb-source.md`, which *is* issue
  #7's source text.
- **Absolute, never relative** — that application's Decision 9: *"1e-6 absolute — float noise only.
  No relative band. Every suppressed write is recorded and attributed to its series"*, with *"the
  current `1e-4·|old|` band must not be ported."* Note `1e-6 absolute` is that application's choice
  of *argument*; the toolkit's default remains `exact()`.
- **The failure being avoided** — a change-detection band of `max(1e-6, 1e-4·|old|)`, i.e. a 0.5-unit
  blind spot on a 5,000-unit series, whose downstream quality checker was widened to match so it
  stopped flagging what storage refused to persist. Measured live at 14 of 111 points permanently
  unhealable. The band's stated justification — that an API rounded values to two decimals — was
  later disproven: no such rounding exists on the value path.
- **Equality including `annotation` and `changed_by`, and `NaN == NaN`** — timedb's native exact
  comparison, `timedb/write.py:228`.
- **OVERLAPPING bypassing dedup** — the carry-over rules, and independently the behaviour note on
  the application's own in-place fix PR (*"dedup disabled for `dtype == model_forecast`"*).
- **Fail-open** — the carry-over rules: *"dedup is fail-open — an exception returns the unfiltered
  batch so dedup can never block an ingest."*

## Deliberate divergences from today's upstream

Recorded so they are choices rather than accidents.

1. **`unchanged_scope="auto"` resolves per series; upstream's is a per-call flag.** Upstream
   `energydb`/`timedb` expose `skip_unchanged` and `unchanged_scope`, and forwarding
   `unchanged_scope` verbatim is a known unfiled bug: it is a per-call flag for what is a per-series
   property, so a manifest mixing `FLAT` and `OVERLAPPING` series cannot be written correctly with
   either setting. Exposure was measured at 26 of 6,426 series, all forecast — small, but exactly
   the series where dropping a republication is unrecoverable. The toolkit reads
   `timeseries_type` from the catalog and can therefore do the right thing per series, which a
   per-call flag structurally cannot. The names are mirrored so call sites survive a future move to
   a real connector; the resolution differs, and better.
2. **No relative tolerance is offered at all.** Upstream permits the caller to pass any comparison;
   the toolkit exposes only `exact()` and an absolute `tolerance()`, because the relative form is
   the recorded defect.
3. **`timeseries_type` is validated.** `TIMESERIES_TYPES` exists but is dead code, and
   `bigquery.register_series` accepts an unvalidated string. This store validates it, because
   suppression behaviour depends on it.

## Out of scope

- Any connection to a real EnergyDB, ClickHouse, or platform endpoint.
- A relative-tolerance comparison mode.
- Migrating `bigquery.py`'s series methods onto the shared helpers beyond the two moved for reuse;
  its behaviour is unchanged.
- `Index.from_dict` tolerance, `violation_message` header arithmetic, and the other minors deferred
  on this branch — tracked in PR #8's review, not reopened here.
