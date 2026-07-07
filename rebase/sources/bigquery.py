"""Google BigQuery data source.

Auth: Application Default Credentials by default (a workspace/customer service account, or
Workload Identity when the deployed function runs on GCP). A service-account JSON path can be
supplied explicitly via ``credentials_path`` or ``GOOGLE_APPLICATION_CREDENTIALS``.
"""

from __future__ import annotations

from typing import Any

from rebase._optional import optional_module
from rebase.sources.base import ConnectorSpec, DataSource, Frame, WriteResult

_SPEC = ConnectorSpec(
    provider="bigquery",
    fields={
        "project": ("BIGQUERY_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"),
        "location": ("BIGQUERY_LOCATION",),
        "credentials_path": ("GOOGLE_APPLICATION_CREDENTIALS",),
    },
)


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

    def _read_frame(self, query: str, params: Any | None = None) -> Frame:
        client = self._get_client()
        job_config = None
        if params:
            bigquery = optional_module("google.cloud.bigquery", "bigquery")
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter(name, _bq_type(value), value) for name, value in params.items()
                ]
            )
        return client.query(query, job_config=job_config).to_dataframe()

    def write(self, df: Frame, table: str, *, mode: str = "append") -> WriteResult:
        bigquery = optional_module("google.cloud.bigquery", "bigquery")
        client = self._get_client()
        disposition = "WRITE_TRUNCATE" if mode == "replace" else "WRITE_APPEND"
        job_config = bigquery.LoadJobConfig(write_disposition=disposition)
        client.load_table_from_dataframe(df, table, job_config=job_config).result()
        return WriteResult(table=table, rows_written=len(df), mode=mode)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _bq_type(value: Any) -> str:
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT64"
    if isinstance(value, float):
        return "FLOAT64"
    return "STRING"
