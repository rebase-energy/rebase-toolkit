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

import os
import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

# Frames are pandas DataFrames at runtime. We keep the annotation as ``Any`` so the toolkit
# core stays import-light: pandas ships only with the per-warehouse extras, never as a core
# dependency, mirroring how ``rebase.data``/``rebase.modeling`` gate their heavy deps.
Frame = Any


class DataSourceError(RuntimeError):
    """Raised when a data source is misconfigured or a warehouse call fails."""


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a :meth:`DataSource.write` call."""

    table: str
    rows_written: int
    mode: str


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


def _resolve_now() -> datetime:
    # Isolated so tests can monkeypatch a deterministic ingestion clock.
    return datetime.now(UTC)


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
    :meth:`write`. The base class layers the bitemporal mapping and context-manager lifecycle
    on top so every warehouse behaves identically to callers.
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
    def write(self, df: Frame, table: str, *, mode: str = "append") -> WriteResult:
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

    def read_bitemporal(self, query: str, spec: BitemporalSpec, *, params: Any | None = None) -> Frame:
        """Run ``query`` and normalise it to canonical ``valid_time``/``knowledge_time`` columns.

        The returned frame is ready to feed to ``emflow`` for a leakage-safe backtest.
        """
        return apply_bitemporal(self.read(query, params=params), spec)

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
