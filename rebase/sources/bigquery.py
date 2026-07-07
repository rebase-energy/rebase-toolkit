"""Google BigQuery data source.

Auth: Application Default Credentials by default (a workspace/customer service account, or
Workload Identity when the deployed function runs on GCP). A service-account JSON path can be
supplied explicitly via ``credentials_path`` or ``GOOGLE_APPLICATION_CREDENTIALS``.

Besides generic ``read``/``write``, this source speaks rebase's canonical energy layout
(see :mod:`rebase.sources.energy`): ``ensure_energy_schema`` creates the ``series_values``
+ ``series`` tables, ``write_series`` appends SIMPLE/VERSIONED series, and ``read_series``
does point-in-time reads (latest / every-issue / as-of) with timedb semantics.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from rebase._optional import optional_module
from rebase.sources.base import ConnectorSpec, DataSource, DataSourceError, Frame, WriteResult
from rebase.sources.energy import (
    SERIES_CATALOG_COLUMNS,
    SeriesKey,
    build_values_rows,
    series_values_select,
)

_SPEC = ConnectorSpec(
    provider="bigquery",
    fields={
        "project": ("BIGQUERY_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"),
        "location": ("BIGQUERY_LOCATION",),
        "credentials_path": ("GOOGLE_APPLICATION_CREDENTIALS",),
    },
)

# Mirrors timedb's ClickHouse series_values: same columns, three time axes, append-only.
# Partition on valid_time months; cluster mirrors the CH sort key (BigQuery's 4-col max).
_SERIES_VALUES_DDL = """
CREATE TABLE IF NOT EXISTS `{dataset}.series_values` (
    series_id      INT64     NOT NULL,
    valid_time     TIMESTAMP NOT NULL,
    knowledge_time TIMESTAMP NOT NULL,
    change_time    TIMESTAMP NOT NULL,
    value          FLOAT64,
    valid_time_end TIMESTAMP,
    run_id         INT64,
    changed_by     STRING,
    annotation     STRING,
    retention      STRING
)
PARTITION BY TIMESTAMP_TRUNC(valid_time, MONTH)
CLUSTER BY series_id, valid_time, knowledge_time, change_time
"""

_SERIES_CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS `{dataset}.series` (
    series_id       INT64     NOT NULL,
    path            STRING    NOT NULL,
    data_type       STRING    NOT NULL,
    name            STRING    NOT NULL,
    canonical_unit  STRING,
    timeseries_type STRING,
    retention       STRING,
    description     STRING,
    inserted_at     TIMESTAMP
)
CLUSTER BY series_id
"""

_REGISTER_MERGE = """
MERGE `{dataset}.series` AS target
USING (
    SELECT @series_id AS series_id, @path AS path, @data_type AS data_type, @name AS name,
           @canonical_unit AS canonical_unit, @timeseries_type AS timeseries_type,
           @retention AS retention, @description AS description,
           CURRENT_TIMESTAMP() AS inserted_at
) AS source
ON target.series_id = source.series_id
WHEN NOT MATCHED THEN INSERT ({columns}) VALUES ({values})
"""


class BigQuerySource(DataSource):
    provider = "bigquery"

    def __init__(self, *, connection: str | None = None, **overrides: Any) -> None:
        settings = _SPEC.resolve(connection, overrides)
        super().__init__(connection=connection, settings=settings)
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            bigquery = optional_module("google.cloud.bigquery", "bigquery")
            kwargs: dict[str, Any] = {}
            if self.settings.get("project"):
                kwargs["project"] = self.settings["project"]
            if self.settings.get("location"):
                kwargs["location"] = self.settings["location"]
            credentials_path = self.settings.get("credentials_path")
            if credentials_path:
                self._client = bigquery.Client.from_service_account_json(credentials_path, **kwargs)
            else:
                self._client = bigquery.Client(**kwargs)
        return self._client

    def _run_query(self, query: str, params: dict[str, Any] | None = None) -> Any:
        client = self._get_client()
        job_config = None
        if params:
            bigquery = optional_module("google.cloud.bigquery", "bigquery")
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter(name, _bq_type(value), value) for name, value in params.items()
                ]
            )
        return client.query(query, job_config=job_config)

    def _read_frame(self, query: str, params: Any | None = None) -> Frame:
        return self._run_query(query, params).to_dataframe()

    def write(self, df: Frame, table: str, *, mode: str = "append") -> WriteResult:
        bigquery = optional_module("google.cloud.bigquery", "bigquery")
        client = self._get_client()
        disposition = "WRITE_TRUNCATE" if mode == "replace" else "WRITE_APPEND"
        job_config = bigquery.LoadJobConfig(write_disposition=disposition)
        client.load_table_from_dataframe(df, table, job_config=job_config).result()
        return WriteResult(table=table, rows_written=len(df), mode=mode)

    # --- canonical energy layout (timedatamodel/energydatamodel conventions) ---------

    def ensure_energy_schema(self, dataset: str) -> None:
        """Create the canonical ``series_values`` + ``series`` tables (idempotent)."""
        self._run_query(f"CREATE SCHEMA IF NOT EXISTS `{dataset}`").result()
        self._run_query(_SERIES_VALUES_DDL.format(dataset=dataset)).result()
        self._run_query(_SERIES_CATALOG_DDL.format(dataset=dataset)).result()

    def register_series(
        self,
        dataset: str,
        *,
        path: str,
        data_type: str,
        name: str,
        unit: str = "dimensionless",
        timeseries_type: str = "FLAT",
        retention: str = "forever",
        description: str | None = None,
    ) -> SeriesKey:
        """Register a series in the catalog (idempotent) and return its key.

        Keying follows energydb: a series is ``(path, data_type, name)`` — e.g.
        ``("portfolio/site-1/t01", "forecast", "electricity.supply")``. ``series_id``
        is derived deterministically from the key, so re-registration is a no-op.
        """
        key = SeriesKey(path=path, data_type=data_type, name=name)
        sql = _REGISTER_MERGE.format(
            dataset=dataset,
            columns=", ".join(SERIES_CATALOG_COLUMNS),
            values=", ".join(f"source.{column}" for column in SERIES_CATALOG_COLUMNS),
        )
        self._run_query(
            sql,
            {
                "series_id": key.series_id,
                "path": key.path,
                "data_type": key.data_type,
                "name": key.name,
                "canonical_unit": unit,
                "timeseries_type": timeseries_type,
                "retention": retention,
                "description": description or "",
            },
        ).result()
        return key

    def write_series(
        self,
        dataset: str,
        data: Any,
        *,
        path: str,
        data_type: str,
        name: str,
        retention: str = "forever",
        changed_by: str = "",
        annotation: str = "",
    ) -> WriteResult:
        """Append a SIMPLE or VERSIONED series to ``series_values``.

        ``data`` is a ``TimeSeries`` (energydatamodel/timedatamodel) or a pandas frame
        with ``valid_time``(+``knowledge_time``) as index levels or columns. Corrections
        are new rows — timedb's read semantics pick the winner at query time.
        """
        key = SeriesKey(path=path, data_type=data_type, name=name)
        rows = build_values_rows(data, key, retention=retention, changed_by=changed_by, annotation=annotation)
        return self.write(rows, f"{dataset}.series_values", mode="append")

    def read_series(
        self,
        dataset: str,
        keys: Any,
        *,
        start_valid: _dt.datetime | None = None,
        end_valid: _dt.datetime | None = None,
        as_of: _dt.datetime | None = None,
        overlapping: bool = False,
        include_updates: bool = False,
    ) -> Frame:
        """Point-in-time read with timedb semantics.

        - default: latest view — one row per valid_time (latest issue, latest correction),
        - ``as_of``: only what was knowable then (bounds ``knowledge_time``),
        - ``overlapping=True``: every forecast issue (adds ``knowledge_time``),
        - ``include_updates=True``: the full AUDIT trail (adds ``change_time`` etc.).

        ``keys`` is one ``(path, data_type, name)`` tuple / ``SeriesKey`` or a list of
        them. Results carry ``path``/``data_type``/``name`` columns — never raw ids.
        """
        resolved = _series_keys(keys)
        by_id = {key.series_id: key for key in resolved}
        sql, params = series_values_select(
            f"`{dataset}.series_values`",
            list(by_id),
            start_valid=start_valid,
            end_valid=end_valid,
            as_of=as_of,
            overlapping=overlapping,
            include_updates=include_updates,
        )
        df = self.read(sql, params=params)
        df.insert(0, "name", df["series_id"].map(lambda sid: by_id[sid].name))
        df.insert(0, "data_type", df["series_id"].map(lambda sid: by_id[sid].data_type))
        df.insert(0, "path", df["series_id"].map(lambda sid: by_id[sid].path))
        return df.drop(columns=["series_id"])

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _series_keys(keys: Any) -> list[SeriesKey]:
    if isinstance(keys, (SeriesKey, tuple)):
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


def _bq_type(value: Any) -> str:
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT64"
    if isinstance(value, float):
        return "FLOAT64"
    if isinstance(value, _dt.datetime):
        return "TIMESTAMP"
    return "STRING"
