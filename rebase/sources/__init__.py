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

from typing import Any

from rebase.sources.base import (
    BitemporalSpec,
    DataSource,
    DataSourceError,
    WriteResult,
)


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


__all__ = [
    "BitemporalSpec",
    "DataSource",
    "DataSourceError",
    "WriteResult",
    "bigquery",
    "databricks",
    "fabric",
    "snowflake",
]
