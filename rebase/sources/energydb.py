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
import os
import re
import warnings
from datetime import UTC, datetime
from typing import Any

from rebase.sources.base import (
    _UNSET,
    DataSource,
    DataSourceError,
    Frame,
    KnowledgeTime,
    SignalOutcome,
    _replay_knowledge_time,
)
from rebase.sources.energy import (
    RETENTION_TIERS,
    SERIES_CATALOG_COLUMNS,
    TIMESERIES_TYPES,
    Change,
    OnNull,
    SeriesKey,
    SeriesWriteResult,
    _as_utc_bound,
    attach_series_keys,
    build_values_rows,
    select_current_state,
    select_series_winners,
    series_keys,
    suppress_rows,
)

_logger = logging.getLogger("rebase.sources")

_MONTH_FORMAT = "%Y-%m"
_CHANGE_TIME_FORMAT = "%Y%m%dT%H%M%S%f"
_SPAN_FORMAT = "%Y%m%dT%H%M%S"
_PARQUET_CONTENT_TYPE = "application/vnd.apache.parquet"
_UNCHANGED_SCOPES = ("auto", "valid_time", "knowledge_time")

#: ``{change_time}Z-{run_id}-{valid_time_min}-{valid_time_max}-{digest}.parquet``
_KEY_SPAN_PATTERN = re.compile(r"^\d{8}T\d+Z-\d+-(\d{8}T\d{6})-(\d{8}T\d{6})-[0-9a-f]+\.parquet$")


def _catalog_key(prefix: str, series_id: int) -> str:
    return f"{prefix}/catalog/{series_id}.json"


def _series_prefix(prefix: str, series_id: int) -> str:
    return f"{prefix}/series/{series_id}/"


def _month_prefix(prefix: str, series_id: int, month: str) -> str:
    return f"{_series_prefix(prefix, series_id)}valid_month={month}/"


def _object_key(
    prefix: str,
    series_id: int,
    month: str,
    change_time: Any,
    run_id: int,
    digest: str,
    span: tuple[Any, Any] | None = None,
) -> str:
    """The object key. Content-addressed by ``digest``, which is what makes it collision-free.

    ``change_time`` and ``run_id`` alone are not enough: ``change_time`` comes from
    ``_resolve_now()``, frozen to the replay bound during a replay, so a replay plus a
    caller-supplied ``run_id`` would produce the same key twice and the second ``put`` would
    silently replace the first — destroying an append-only object. With the digest, identical
    content is idempotent and differing content can never collide.

    ``span`` is the object's ``valid_time`` range, written into the key so a later read can tell
    from the *listing* whether an object can possibly contain the rows it wants — see
    :func:`_key_span`. It is derived from the content, so it cannot break the idempotency above:
    identical bytes always yield an identical span and therefore an identical key.
    """
    stamp = change_time.strftime(_CHANGE_TIME_FORMAT)
    window = ""
    if span is not None:
        window = f"{span[0].strftime(_SPAN_FORMAT)}-{span[1].strftime(_SPAN_FORMAT)}-"
    return f"{_month_prefix(prefix, series_id, month)}{stamp}Z-{run_id}-{window}{digest}.parquet"


def _content_span(valid_times: Any) -> tuple[Any, Any]:
    """The ``valid_time`` range of one object, floored and ceiled to whole seconds.

    Rounding outward is deliberate: the key advertises a range that is never *narrower* than
    the rows inside, so an overlap test on it can drop an object but can never drop one that
    held a wanted row. Truncating both ends inward would make pruning lose data.
    """
    return valid_times.min().floor("s"), valid_times.max().ceil("s")


def _key_span(key: str) -> tuple[datetime, datetime] | None:
    """The object's advertised ``valid_time`` range, or ``None`` when the key does not carry one.

    ``None`` means "cannot tell", and every caller must then read the object. A key written
    before this grammar existed, or by hand, must never be pruned away on a guess.
    """
    match = _KEY_SPAN_PATTERN.match(key.rsplit("/", 1)[-1])
    if match is None:
        return None
    try:
        start = datetime.strptime(match.group(1), _SPAN_FORMAT).replace(tzinfo=UTC)
        end = datetime.strptime(match.group(2), _SPAN_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None
    return start, end


def _content_digest(blob: bytes) -> str:
    """The first 12 hex chars of sha256 over the encoded object bytes."""
    import hashlib

    return hashlib.sha256(blob).hexdigest()[:12]


def _overlaps(object_span: tuple[datetime, datetime] | None, wanted: tuple[Any, Any]) -> bool:
    """Whether an object can hold a row in ``wanted``. Unknown spans always overlap."""
    if object_span is None:
        return True
    start, end = object_span
    return end >= _as_utc_bound(wanted[0]) and start <= _as_utc_bound(wanted[1])


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
            mismatch = sorted(set(SERIES_CATALOG_COLUMNS) ^ set(record))
            raise DataSourceError(f"catalog record does not match SERIES_CATALOG_COLUMNS: {mismatch}")
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

    def _raw_rows(
        self,
        series_ids: list[int],
        months: list[str] | None,
        span: tuple[Any, Any] | None = None,
    ) -> Frame:
        """Concatenate the stored objects for these series that can hold the wanted rows.

        Two levels of pruning, both from the *listing* rather than the contents: ``months``
        selects the ``valid_month`` partitions, and ``span`` then drops the objects inside those
        partitions whose advertised ``valid_time`` range cannot overlap what was asked for.

        The second level is what keeps a long-lived month affordable. Every write appends an
        object and nothing is ever rewritten, so without it the cost of a read — including the
        read-before-write behind ``skip_unchanged`` — grows with every write ever made into that
        month, whether or not the object could contain a relevant row. An object whose key does
        not advertise a range is always read; pruning never guesses.
        """
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
                    if span is not None and not _overlaps(_key_span(entry.key), span):
                        continue
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
        with_provenance: bool = False,
    ) -> Frame:
        """Point-in-time read with EnergyDB semantics, in the shape a real query returns.

        - default: the latest view — one row per ``valid_time`` (latest issue, latest
          correction within it),
        - ``as_of``: only what was knowable then (bounds ``knowledge_time``); defaults to the
          replay bound during a replay,
        - ``overlapping=True``: every forecast issue (adds ``knowledge_time``),
        - ``include_updates=True``: the full AUDIT trail,
        - ``with_provenance=True``: the same winners as the default view plus ``annotation``
          and ``changed_by``. Those two are per-row, so a per-point quality flag is only
          readable back through this (or the audit trail). Without it, asking "which stored
          points are flagged" means pulling every revision of every row and re-deriving the
          winners by hand. Ignored under ``include_updates``, whose shape already carries both.

        ``keys`` is one ``(path, data_type, name)`` tuple / :class:`SeriesKey` or a list of
        them. Results carry ``path``/``data_type``/``name`` — never raw ids.
        """
        resolved = series_keys(keys)
        by_id = {key.series_id: key for key in resolved}
        months = _months_in_range(start_valid, end_valid)
        span = (start_valid, end_valid) if start_valid is not None and end_valid is not None else None
        raw = self._raw_rows(list(by_id), months, span)
        if start_valid is not None and len(raw):
            raw = raw[raw["valid_time"] >= _as_utc_bound(start_valid)]
        if end_valid is not None and len(raw):
            raw = raw[raw["valid_time"] < _as_utc_bound(end_valid)]
        if with_provenance and not include_updates:
            winners = select_current_state(raw, overlapping=overlapping, as_of=as_of)
        else:
            winners = select_series_winners(raw, overlapping=overlapping, include_updates=include_updates, as_of=as_of)
        return attach_series_keys(winners, by_id)

    def write_series(
        self,
        data: Any,
        key: SeriesKey,
        *,
        retention: str = "forever",
        changed_by: str = "",
        annotation: str = "",
        run_id: int | str | None = None,
        knowledge_time: KnowledgeTime | None = None,
        skip_unchanged: bool = False,
        unchanged_scope: str = "auto",
        change: Change | None = None,
        on_null: OnNull = OnNull.KEEP_STORED,
        dataset: Any = None,
        contract: Any = None,
        on_violation: str | None = None,
        validate: bool = True,
        watermark: Any = _UNSET,
    ) -> SeriesWriteResult:
        """Append a SIMPLE or VERSIONED series, honouring the declared write semantics.

        ``skip_unchanged`` suppresses no-op rewrites; ``change`` declares what "unchanged"
        means (absolute tolerance only, defaulting to exact); ``on_null`` decides whether an
        incoming null may replace a stored value. ``unchanged_scope="auto"`` resolves per
        series from the catalog, so a *registered* ``OVERLAPPING`` series bypasses suppression
        entirely — every publication of a forecast is meaningful. An explicit ``"valid_time"``/
        ``"knowledge_time"`` override widens both the stored-state read below and the
        suppression lookup key in :func:`~rebase.sources.energy.suppress_rows` to
        ``(valid_time, knowledge_time)``, so each forecast issue is compared against its own
        prior issue instead of one issue winning the whole ``valid_time``; it can never switch
        off ``on_null=KEEP_STORED`` protection, which is decided by catalog truth alone.

        Suppression is fail-open: if anything in that path raises, the unfiltered batch is
        written and ``fail_open`` is set. Suppression is an optimisation, never a gate.

        ``dataset`` and/or ``contract`` run the same ``validate -> write -> signal`` pipeline
        :meth:`DataSource.write` runs, so a store-backed write is governed like a warehouse
        one: the contract comes from ``contract=`` or the dataset's, a failure under
        ``on_violation="fail"`` refuses the write outright, ``"warn"`` lands the batch and
        records the failure, and the dataset is signalled afterwards with the validation report
        so ``OnUpdate(only_valid=True)`` does not fire downstream work on a batch that failed.
        The signal is best-effort — it never fails a write that already happened — and is
        suppressed during replays.

        Validation runs on the **expanded** ``series_values`` rows, after the ``knowledge_time``
        stamp so a contract may require that column, and **before** suppression: a contract
        describes what the upstream delivered, while suppression is a storage optimisation that
        removes rows precisely because they are already stored. Validating afterwards would
        measure gaps and row order in a frame dedup had punched holes in.
        """
        if unchanged_scope not in _UNCHANGED_SCOPES:
            raise DataSourceError(f"unchanged_scope must be one of {_UNCHANGED_SCOPES}; got {unchanged_scope!r}")
        if not isinstance(on_null, OnNull):
            raise DataSourceError(f"on_null must be an OnNull member; got {on_null!r}")
        resolved_keys = series_keys(key)
        if len(resolved_keys) != 1:
            raise DataSourceError(f"write_series writes exactly one series; got {len(resolved_keys)}")
        key = resolved_keys[0]

        if knowledge_time is not None:
            if not isinstance(knowledge_time, KnowledgeTime):
                raise DataSourceError(
                    "knowledge_time must be a KnowledgeTime (e.g. KnowledgeTime.from_source(...), "
                    f".from_inputs(...) or .at(...)); got {type(knowledge_time)!r}"
                )
            data = knowledge_time.apply(data)
        rows = build_values_rows(
            data, key, retention=retention, changed_by=changed_by, annotation=annotation, run_id=run_id
        )

        resolved_dataset, resolved_contract = self._resolve_contract(dataset, contract, validate=validate)
        validation = self._validate_rows(
            rows,
            resolved_contract,
            dataset_name=getattr(resolved_dataset, "name", "") or f"{key.path}/{key.data_type}/{key.name}",
            on_violation=on_violation,
            validate=validate,
        )
        if watermark is _UNSET:
            watermark = DataSource._derive_watermark(rows, resolved_contract)
        watermark_value = watermark

        # Only a genuinely registered OVERLAPPING series may bypass suppression entirely — every
        # publication is meaningful there. An explicit unchanged_scope override changes only the
        # comparison partition passed to select_current_state and suppress_rows below; it never
        # overrides catalog truth for whether on_null=KEEP_STORED protection applies.
        #
        # The catalog lookup itself is gated behind needs_overlap_check: when skip_unchanged is
        # False and on_null is WRITE_NULL, must_compare is False no matter what the catalog says
        # (see below), so a *registered* series must not still pay for a catalog get it will
        # never use — that get is the other read this write path can skip.
        needs_overlap_check = skip_unchanged or on_null is OnNull.KEEP_STORED
        is_registered_overlapping = self._is_overlapping(key.series_id) if needs_overlap_check else False
        if unchanged_scope == "auto":
            partition_overlapping = is_registered_overlapping
        else:
            partition_overlapping = unchanged_scope == "knowledge_time"

        report: dict[str, Any] = {"suppressed_unchanged": 0, "suppressed_null": 0, "sample_valid_times": ()}
        fail_open = False
        # The read is the write path's only added cost, so it is skipped in two cases: (1) a
        # genuinely registered OVERLAPPING series, where nothing is ever suppressed; and (2)
        # skip_unchanged=False with on_null=WRITE_NULL, where the only rule this could miss is
        # "stored null + incoming null -> skip" (rule 2) — skipping the read means that redundant
        # null gets written as a harmless duplicate rather than suppressed, never a lost
        # correction.
        must_compare = not is_registered_overlapping and needs_overlap_check
        if must_compare:
            try:
                months = sorted({stamp.strftime(_MONTH_FORMAT) for stamp in rows["valid_time"]})
                # Only the batch's own valid_times can decide any of the seven rules, so the
                # stored-state read is bounded to that span — including the null-overwrite
                # check, which asks about the same valid_times from whichever issue wins there.
                stored = select_current_state(
                    self._raw_rows([key.series_id], months, _content_span(rows["valid_time"])),
                    overlapping=partition_overlapping,
                )
                rows, report = suppress_rows(
                    rows,
                    stored,
                    on_null=on_null,
                    skip_unchanged=skip_unchanged,
                    change=change,
                    partition_overlapping=partition_overlapping,
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
                # Encode first: the digest must be over the exact bytes being stored, so that
                # identical content always resolves to the identical key (idempotent) and
                # differing content never collides — even under a replay's frozen change_time.
                blob = _encode_parquet(group)
                object_key = _object_key(
                    self.prefix,
                    key.series_id,
                    str(month),
                    group["change_time"].iloc[0],
                    int(group["run_id"].iloc[0]),
                    _content_digest(blob),
                    _content_span(group["valid_time"]),
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
            validation=validation,
            signal=self._signal(resolved_dataset, validation, watermark_value),
            watermark=watermark_value,
        )

    # --- the validate -> write -> signal pipeline, shared with DataSource.write ------------

    @staticmethod
    def _resolve_contract(dataset: Any, contract: Any, *, validate: bool) -> tuple[Any, dict | None]:
        """The dataset object (if any) and the contract dict to validate against.

        An explicit ``contract=`` wins over the dataset's, so a caller can validate without a
        dataset at all — the local case, where nothing is registered on the platform yet.
        """
        from rebase.client import Dataset, _coerce_config_dict

        resolved_dataset = Dataset.from_name(dataset) if isinstance(dataset, str) else dataset
        if not validate:
            return resolved_dataset, None
        if contract is not None:
            return resolved_dataset, _coerce_config_dict(contract, field_name="contract")
        if resolved_dataset is None:
            return None, None
        stored = getattr(resolved_dataset, "contract", None)
        if stored is None:
            fetch = getattr(resolved_dataset, "_stored_contract", None)
            if callable(fetch):
                stored = fetch()  # cached per instance; fetch errors are swallowed there
        return resolved_dataset, _coerce_config_dict(stored, field_name="contract")

    @staticmethod
    def _validate_rows(
        rows: Frame,
        contract: dict | None,
        *,
        dataset_name: str,
        on_violation: str | None,
        validate: bool,
    ) -> Any:
        """Validate, raising under ``on_violation="fail"``. Returns the report, or ``None``."""
        from rebase.contract import ContractViolation, ValidationReport, validate_frame, violation_message

        if not validate:
            return ValidationReport(passed=False, checks=0, row_count=int(len(rows)), failures=[], skipped=True)
        if not contract:
            return None
        report = validate_frame(rows, contract, dataset_name=dataset_name)
        if report.passed:
            return report
        policy = on_violation or (contract.get("x-rebase") or {}).get("on_violation") or "fail"
        if policy not in {"fail", "warn"}:
            raise DataSourceError(f"on_violation must be 'fail' or 'warn', got {policy!r}")
        if policy == "fail":
            raise ContractViolation(violation_message(dataset_name, report, nothing_written=True), report=report)
        _logger.warning(
            "energydb: %s failed %d of %d contract checks; writing anyway (on_violation='warn')",
            dataset_name,
            len(report.failures),
            report.checks,
        )
        return report

    @staticmethod
    def _signal(dataset: Any, report: Any, watermark: Any) -> Any:
        """Signal the dataset. Best-effort: the rows are already written either way."""
        if dataset is None:
            return None
        if _replay_knowledge_time() is not None:
            return SignalOutcome(sent=False, error="suppressed: replay")
        kwargs = {
            "watermark": watermark,
            "validation": report.to_payload() if report is not None else None,
            "source": "energydb_write",
            "run_id": os.environ.get("REBASE_RUN_ID"),
        }
        try:
            try:
                response = dataset.mark_updated(**kwargs)
            except Exception:  # noqa: BLE001 - one blanket retry keeps transient API hiccups quiet
                response = dataset.mark_updated(**kwargs)
            fired = list(response.get("fired") or []) if isinstance(response, dict) else []
            return SignalOutcome(sent=True, fired=fired)
        except Exception as exc:  # noqa: BLE001 - the write already succeeded; only warn
            warnings.warn(f"energydb: dataset signal after writing {dataset} failed: {exc}", stacklevel=2)
            return SignalOutcome(sent=False, error=repr(exc))
