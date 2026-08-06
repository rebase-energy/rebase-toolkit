"""Shared contract for Rebase warehouse data sources.

A :class:`DataSource` connects deployed Rebase code (a ``Predictor.fit``/``predict``,
a ``@project.step`` or a ``@project.workflow``) to a customer data warehouse such as
Snowflake, Databricks, BigQuery or Microsoft Fabric.

The connector runs *inside* the deployed function, where the platform already injects
credentials as environment variables (via ``secrets=``/``env=`` on functions, models and
workflows). Nothing about the warehouse touches the Rebase control plane; the toolkit only
provides a uniform ``read``/``read_bitemporal``/``write`` surface plus a leakage-safe
bitemporal mapping so that data pulled from a warehouse can be backtested honestly with
``emflow``.
"""

from __future__ import annotations

import logging
import os
import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rebase.client import Dataset

# Frames are pandas DataFrames at runtime. We keep the annotation as ``Any`` so the toolkit
# core stays import-light: pandas ships only with the per-warehouse extras, never as a core
# dependency, mirroring how ``rebase.data``/``rebase.modeling`` gate their heavy deps.
Frame = Any

_UNSET = object()

_logger = logging.getLogger("rebase.sources")


class DataSourceError(RuntimeError):
    """Raised when a data source is misconfigured or a warehouse call fails."""


@dataclass(frozen=True)
class SignalOutcome:
    """Whether the post-write dataset signal was delivered, and what it fired."""

    sent: bool
    error: str | None = None
    fired: list = field(default_factory=list)


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a :meth:`DataSource.write` call."""

    table: str
    rows_written: int
    mode: str
    validation: Any = None  # rebase.ValidationReport | None
    signal: Any = None  # SignalOutcome | None
    watermark: Any = None


@dataclass(frozen=True)
class BitemporalSpec:
    """How to map a warehouse table onto Rebase's leakage-safe bitemporal model.

    Every observation in a backtest carries two timestamps: the ``valid_time`` (the target /
    event time the row describes) and the ``knowledge_time`` (the moment the row became
    knowable). ``emflow`` uses ``knowledge_time`` to make leakage *structurally impossible* —
    a model physically cannot read a row whose ``knowledge_time`` is after the forecast origin.

    A raw warehouse table usually only has a target timestamp, so you must declare how to
    derive ``knowledge_time``:

    * ``knowledge_time`` — the column that records when the row became available (issue time of
      a forecast, arrival time of a meter reading). Strongly preferred.
    * ``knowledge_delay`` — if there is no such column but availability lags the target time by a
      known, fixed amount (e.g. actuals land 1 hour after the hour), set this delay and
      ``knowledge_time`` is computed as ``valid_time + knowledge_delay``.
    * neither — ``knowledge_time`` falls back to *ingestion time* (now, UTC). This is only safe
      for data that is known effectively immediately; otherwise it silently leaks future
      information into backtests, so the connector emits a warning.
    """

    valid_time: str
    knowledge_time: str | None = None
    knowledge_delay: timedelta | None = None

    def __post_init__(self) -> None:
        if self.knowledge_time is not None and self.knowledge_delay is not None:
            raise ValueError("Set at most one of knowledge_time or knowledge_delay, not both.")


def _replay_knowledge_time() -> datetime | None:
    """The knowledge-time bound of the current replay run, or ``None`` outside replays.

    Replay runs are launched with ``REBASE_REPLAY_KNOWLEDGE_TIME`` set to the original
    run's creation time (ISO 8601, UTC assumed when naive). Parsed on every call — never
    cached at import — so a process can observe the variable appearing or changing.
    """
    raw = os.environ.get("REBASE_REPLAY_KNOWLEDGE_TIME")
    if not raw:
        return None
    try:
        bound = datetime.fromisoformat(raw)
    except ValueError:
        warnings.warn(
            f"REBASE_REPLAY_KNOWLEDGE_TIME is not an ISO 8601 datetime: {raw!r}; ignoring the replay bound.",
            stacklevel=2,
        )
        return None
    if bound.tzinfo is None:
        bound = bound.replace(tzinfo=UTC)
    return bound


def _resolve_now() -> datetime:
    # Isolated so tests can monkeypatch a deterministic ingestion clock. During a replay
    # the ingestion-time fallback stamps the replay's knowledge-time bound instead of
    # wall-clock now, so the post-read replay filter keeps fallback rows.
    return _replay_knowledge_time() or datetime.now(UTC)


def apply_bitemporal(df: Frame, spec: BitemporalSpec) -> Frame:
    """Normalise ``df`` in place to canonical ``valid_time``/``knowledge_time`` columns.

    Returns the same DataFrame with ``valid_time`` (renamed from ``spec.valid_time``) and a
    populated ``knowledge_time`` column, ready to hand to ``emflow``.
    """

    import pandas as pd

    if spec.valid_time not in df.columns:
        raise DataSourceError(
            f"Bitemporal valid_time column {spec.valid_time!r} not found in result columns {list(df.columns)}."
        )

    if spec.valid_time != "valid_time":
        df = df.rename(columns={spec.valid_time: "valid_time"})
    df["valid_time"] = pd.to_datetime(df["valid_time"], utc=True)

    if spec.knowledge_time is not None:
        if spec.knowledge_time not in df.columns:
            raise DataSourceError(
                f"Bitemporal knowledge_time column {spec.knowledge_time!r} not found in "
                f"result columns {list(df.columns)}."
            )
        if spec.knowledge_time != "knowledge_time":
            df = df.rename(columns={spec.knowledge_time: "knowledge_time"})
        df["knowledge_time"] = pd.to_datetime(df["knowledge_time"], utc=True)
    elif spec.knowledge_delay is not None:
        df["knowledge_time"] = df["valid_time"] + spec.knowledge_delay
    else:
        warnings.warn(
            "No knowledge_time column or knowledge_delay set on BitemporalSpec; falling back to "
            "ingestion time. Backtests on this source may leak future information unless the data "
            "is genuinely known at its valid_time.",
            stacklevel=2,
        )
        df["knowledge_time"] = _resolve_now()

    return df


def resolve_settings(
    connection: str | None,
    fields: dict[str, tuple[str, ...]],
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """Resolve connector settings from explicit kwargs, then env, in priority order.

    For each logical ``field`` the lookup order is:

    1. an explicit keyword passed to the factory (``overrides``),
    2. ``REBASE_SOURCE_<CONNECTION>_<FIELD>`` when ``connection`` is given,
    3. any provider-standard env var aliases declared in ``fields``.

    ``fields`` maps each logical field name to a tuple of provider-standard env var names to try
    (may be empty). Only non-``None`` values are returned, so callers apply their own defaults
    and required-field checks.
    """

    resolved: dict[str, Any] = {}
    conn_token = connection.upper().replace("-", "_") if connection else None

    for name, env_aliases in fields.items():
        if name in overrides and overrides[name] is not None:
            resolved[name] = overrides[name]
            continue

        value: str | None = None
        if conn_token is not None:
            value = os.environ.get(f"REBASE_SOURCE_{conn_token}_{name.upper()}")
        if value is None:
            for alias in env_aliases:
                value = os.environ.get(alias)
                if value is not None:
                    break

        if value is not None:
            resolved[name] = value

    return resolved


class DataSource(ABC):
    """Uniform read/write surface over a customer data warehouse.

    Subclasses implement :meth:`_read_frame` (run a query, return a pandas DataFrame) and
    :meth:`_write`. The base class layers the bitemporal mapping, dataset signalling and
    context-manager lifecycle on top so every warehouse behaves identically to callers.
    """

    #: Short provider identifier, e.g. ``"snowflake"``.
    provider: str = "warehouse"

    def __init__(self, *, connection: str | None = None, settings: dict[str, Any] | None = None) -> None:
        self.connection = connection
        self.settings = settings or {}

    # --- to implement per warehouse -------------------------------------------------

    @abstractmethod
    def _read_frame(self, query: str, params: Any | None = None) -> Frame:
        """Execute ``query`` and return the result as a pandas DataFrame."""

    @abstractmethod
    def _write(self, df: Frame, table: str, mode: str) -> WriteResult:
        """Write a DataFrame to ``table``. ``mode`` is ``"append"`` or ``"replace"``."""

    def close(self) -> None:  # noqa: B027 - optional override; sources without a live handle need no cleanup
        """Release any underlying connection. No-op by default."""

    # --- shared surface -------------------------------------------------------------

    def read(self, query: str, *, params: Any | None = None) -> Frame:
        """Run ``query`` against the warehouse and return a pandas DataFrame."""
        try:
            return self._read_frame(query, params)
        except DataSourceError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise every driver's error type
            raise DataSourceError(f"{self.provider} read failed: {exc}") from exc

    def write(
        self,
        df: Frame,
        table: str,
        *,
        mode: str = "append",
        dataset: Dataset | str | None = None,
        watermark: Any = _UNSET,
        on_violation: str | None = None,
        validate: bool = True,
    ) -> WriteResult:
        """Write a DataFrame to ``table``. ``mode`` is ``"append"`` or ``"replace"``.

        Pass ``dataset`` (a :class:`rebase.Dataset` or a dataset name) to run the pipeline
        ``resolve contract -> validate -> write -> signal``: the frame is validated against
        the dataset's contract (in-code, else the one stored on the platform), the write only
        happens when validation passes (or the effective policy is ``"warn"``), and the
        dataset is signalled afterwards with the validation report and a watermark. The
        signal is best-effort: a failure never fails the write itself.

        ``watermark`` overrides the value derived from the contract's ``watermark_column``
        (pass ``None`` explicitly to signal a null watermark). ``on_violation`` overrides the
        contract's policy (``"fail"`` or ``"warn"``); ``validate=False`` skips validation and
        flags the signal as skipped.
        """
        replay_bound = _replay_knowledge_time()
        if dataset is None:
            if replay_bound is not None:
                _logger.warning(
                    "replay run: writing to %r — guard with ctx.is_replay if replays should not write", table
                )
            return self._write(df, table, mode)

        # 1. Resolve the dataset and its contract.
        # Local import: the toolkit's client stays out of the sources import path.
        from rebase.client import Dataset

        resolved = Dataset.from_name(dataset) if isinstance(dataset, str) else dataset
        dataset_name = getattr(resolved, "name", str(dataset))
        contract: dict | None = None
        if validate:
            contract = getattr(resolved, "contract", None)
            if contract is None:
                fetch = getattr(resolved, "_stored_contract", None)
                if callable(fetch):
                    contract = fetch()  # cached per instance; fetch errors are swallowed
        # Note: the stored format reserves ``x-rebase.require_contract`` for the backend to
        # enforce contract presence on signals; the SDK write path intentionally does not
        # enforce it (an unresolvable contract simply skips validation).

        # 2. Validate.
        report: Any = None
        if not validate:
            from rebase.contract import ValidationReport

            row_count = len(df) if hasattr(df, "__len__") else 0
            report = ValidationReport(passed=False, checks=0, row_count=int(row_count), failures=[], skipped=True)
        elif contract:
            from rebase.contract import ContractViolation, validate_frame, violation_message

            report = validate_frame(df, contract, dataset_name=dataset_name)
            if not report.passed:
                policy = on_violation or (contract.get("x-rebase") or {}).get("on_violation") or "fail"
                if policy not in {"fail", "warn"}:
                    raise DataSourceError(f"on_violation must be 'fail' or 'warn', got {policy!r}")
                if policy == "fail":
                    raise ContractViolation(
                        violation_message(dataset_name, report, nothing_written=True),
                        report=report,
                    )
                _logger.warning(
                    "%s: %s failed %d of %d contract checks; writing anyway (on_violation='warn')",
                    self.provider,
                    dataset_name,
                    len(report.failures),
                    report.checks,
                )

        # 3. Write.
        if replay_bound is not None:
            _logger.warning("replay run: writing to %r — guard with ctx.is_replay if replays should not write", table)
        result = self._write(df, table, mode)

        # 4. Signal (best-effort; suppressed entirely during replays so they never fire triggers).
        watermark_value = watermark if watermark is not _UNSET else self._derive_watermark(df, contract)
        if replay_bound is not None:
            return replace(
                result,
                validation=report,
                signal=SignalOutcome(sent=False, error="suppressed: replay"),
                watermark=watermark_value,
            )
        validation_payload = report.to_payload() if report is not None else None
        run_id = os.environ.get("REBASE_RUN_ID")
        signal_kwargs = {
            "watermark": watermark_value,
            "validation": validation_payload,
            "source": "source_write",
            "run_id": run_id,
        }
        try:
            try:
                response = resolved.mark_updated(**signal_kwargs)
            except Exception:  # noqa: BLE001 - one blanket retry keeps transient API hiccups quiet
                response = resolved.mark_updated(**signal_kwargs)
            fired = list(response.get("fired") or []) if isinstance(response, dict) else []
            signal = SignalOutcome(sent=True, fired=fired)
        except Exception as exc:  # noqa: BLE001 - the write already succeeded; only warn
            warnings.warn(
                f"{self.provider}: dataset signal after writing {table!r} failed: {exc}",
                stacklevel=2,
            )
            signal = SignalOutcome(sent=False, error=repr(exc))
        return replace(result, validation=report, signal=signal, watermark=watermark_value)

    @staticmethod
    def _derive_watermark(df: Frame, contract: dict | None) -> Any:
        """Derive a watermark from the contract's ``x-rebase.watermark_column``, if any."""
        if not contract:
            return None
        column = (contract.get("x-rebase") or {}).get("watermark_column")
        if not column or not hasattr(df, "columns") or column not in df.columns:
            return None
        try:
            value = df[column].max()
        except Exception:  # noqa: BLE001 - a bad column must not block the signal
            return None
        if value is None or value != value:  # NaN/NaT
            return None
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    def read_bitemporal(self, query: str, spec: BitemporalSpec, *, params: Any | None = None) -> Frame:
        """Run ``query`` and normalise it to canonical ``valid_time``/``knowledge_time`` columns.

        The returned frame is ready to feed to ``emflow`` for a leakage-safe backtest.
        During a replay run, rows whose ``knowledge_time`` is after the replay's
        knowledge-time bound are filtered out, so the run sees exactly what the
        original run could have seen.
        """
        df = apply_bitemporal(self.read(query, params=params), spec)
        bound = _replay_knowledge_time()
        if bound is not None:
            df = df[df["knowledge_time"] <= bound]
        return df

    def __enter__(self) -> DataSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass
class ConnectorSpec:
    """Declares a warehouse's settable fields and their provider-standard env var aliases."""

    provider: str
    fields: dict[str, tuple[str, ...]] = field(default_factory=dict)
    required: tuple[str, ...] = ()

    def resolve(self, connection: str | None, overrides: dict[str, Any]) -> dict[str, Any]:
        settings = resolve_settings(connection, self.fields, overrides)
        missing = [name for name in self.required if not settings.get(name)]
        if missing:
            hint = connection or "<name>"
            raise DataSourceError(
                f"{self.provider}: missing required setting(s) {missing}. Pass them to the factory, "
                f"or set REBASE_SOURCE_{hint.upper().replace('-', '_')}_<FIELD> "
                f"(injected via secrets=/env= on your deployed function)."
            )
        return settings
