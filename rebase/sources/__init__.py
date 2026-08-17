"""Warehouse data sources for Rebase.

Connect deployed Rebase code to a customer data warehouse with a uniform read/write surface
and a leakage-safe bitemporal mapping::

    import rebase as rb

    src = rb.sources.bigquery(connection="acme-prod")
    spec = rb.sources.BitemporalSpec(valid_time="ts", knowledge_time="issued_at")
    train = src.read_bitemporal("SELECT ts, issued_at, load FROM demand", spec)

Credentials are resolved from explicit keyword arguments first, then from environment variables
(``REBASE_SOURCE_<CONNECTION>_<FIELD>`` or provider-standard names) which the platform injects
from Secret Manager via ``secrets=``/``env=`` on your function, model or workflow.

Each backend needs its own optional dependency, e.g. ``pip install "rebase-toolkit[snowflake]"``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rebase.sources.base import (
    BitemporalSpec,
    DataSource,
    DataSourceError,
    KnowledgeTime,
    WriteResult,
)
from rebase.sources.energy import Change, OnNull, SeriesKey, SeriesWriteResult

if TYPE_CHECKING:
    from rebase.sources.energydb import EnergyDBStore


def snowflake(*, connection: str | None = None, **overrides: Any) -> DataSource:
    """Create a Snowflake data source. Requires ``rebase-toolkit[snowflake]``."""
    from rebase.sources.snowflake import SnowflakeSource

    return SnowflakeSource(connection=connection, **overrides)


def databricks(*, connection: str | None = None, **overrides: Any) -> DataSource:
    """Create a Databricks data source. Requires ``rebase-toolkit[databricks]``."""
    from rebase.sources.databricks import DatabricksSource

    return DatabricksSource(connection=connection, **overrides)


def bigquery(*, connection: str | None = None, **overrides: Any) -> DataSource:
    """Create a BigQuery data source. Requires ``rebase-toolkit[bigquery]``."""
    from rebase.sources.bigquery import BigQuerySource

    return BigQuerySource(connection=connection, **overrides)


def fabric(*, connection: str | None = None, **overrides: Any) -> DataSource:
    """Create a Microsoft Fabric data source. Requires ``rebase-toolkit[fabric]``."""
    from rebase.sources.fabric import FabricSource

    return FabricSource(connection=connection, **overrides)


# Claim Python's automatic `rebase.sources.energydb` submodule binding here, once, before
# `energydb` is defined below. Without this, the *first* time anything (this module included)
# imports `rebase.sources.energydb`, Python overwrites this module's `energydb` attribute with
# the submodule object itself — permanently shadowing the factory function for the rest of the
# process. Importing the submodule is safe here: it stays pandas/pyarrow-free at import time
# (see its docstring) and only reaches into pandas-touching code inside its own functions.
import rebase.sources.energydb as _energydb_submodule  # noqa: E402, F401


def energydb(*, bucket: Any, prefix: str = "energydb") -> EnergyDBStore:
    """Create a bucket-backed EnergyDB store. Requires ``rebase-toolkit[energydb]``.

    ``bucket`` is a bucket name or an :class:`rb.Bucket`. This backing is transitional — see
    :mod:`rebase.sources.energydb` for what a real connector would replace.
    """
    from rebase.sources.energydb import EnergyDBStore

    return EnergyDBStore(bucket, prefix=prefix)


def __getattr__(name: str) -> Any:
    if name == "EnergyDBStore":
        from rebase.sources.energydb import EnergyDBStore as _EnergyDBStore

        return _EnergyDBStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BitemporalSpec",
    "Change",
    "DataSource",
    "DataSourceError",
    "EnergyDBStore",
    "KnowledgeTime",
    "OnNull",
    "SeriesKey",
    "SeriesWriteResult",
    "WriteResult",
    "bigquery",
    "databricks",
    "energydb",
    "fabric",
    "snowflake",
]
