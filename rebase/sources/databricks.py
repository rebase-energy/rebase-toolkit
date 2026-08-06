"""Databricks (Unity Catalog / SQL warehouse) data source.

Auth: OAuth machine-to-machine (``client_id`` + ``client_secret``) preferred, or a personal
access token (``access_token``). Connect against a SQL warehouse's ``http_path`` on the
workspace ``server_hostname``.

Reads use the Arrow fetch path. ``write`` uses batched ``INSERT`` statements and is intended
for modest result sets (e.g. writing forecasts back); for bulk loads land Parquet in a Unity
Catalog volume and ``COPY INTO`` instead.
"""

from __future__ import annotations

from typing import Any

from rebase._optional import optional_module
from rebase.sources.base import ConnectorSpec, DataSource, DataSourceError, Frame, WriteResult

_SPEC = ConnectorSpec(
    provider="databricks",
    fields={
        "server_hostname": ("DATABRICKS_SERVER_HOSTNAME", "DATABRICKS_HOST"),
        "http_path": ("DATABRICKS_HTTP_PATH",),
        "access_token": ("DATABRICKS_TOKEN", "DATABRICKS_ACCESS_TOKEN"),
        "client_id": ("DATABRICKS_CLIENT_ID",),
        "client_secret": ("DATABRICKS_CLIENT_SECRET",),
        "catalog": ("DATABRICKS_CATALOG",),
        "schema": ("DATABRICKS_SCHEMA",),
    },
    required=("server_hostname", "http_path"),
)

_INSERT_BATCH = 1000


class DatabricksSource(DataSource):
    provider = "databricks"

    def __init__(self, *, connection: str | None = None, **overrides: Any) -> None:
        settings = _SPEC.resolve(connection, overrides)
        super().__init__(connection=connection, settings=settings)
        self._conn: Any | None = None

    def _connect_kwargs(self) -> dict[str, Any]:
        s = self.settings
        kwargs: dict[str, Any] = {"server_hostname": s["server_hostname"], "http_path": s["http_path"]}
        for optional in ("catalog", "schema"):
            if s.get(optional):
                kwargs[optional] = s[optional]

        if s.get("access_token"):
            kwargs["access_token"] = s["access_token"]
        elif s.get("client_id") and s.get("client_secret"):
            kwargs["credentials_provider"] = _oauth_m2m_provider(s)
        else:
            raise DataSourceError("databricks: provide access_token, or client_id + client_secret for OAuth M2M.")
        return kwargs

    def _get_conn(self) -> Any:
        if self._conn is None:
            sql = optional_module("databricks.sql", "databricks")
            self._conn = sql.connect(**self._connect_kwargs())
        return self._conn

    def _read_frame(self, query: str, params: Any | None = None) -> Frame:
        cur = self._get_conn().cursor()
        try:
            cur.execute(query, params)
            return cur.fetchall_arrow().to_pandas()
        finally:
            cur.close()

    def _write(self, df: Frame, table: str, mode: str) -> WriteResult:
        columns = list(df.columns)
        cur = self._get_conn().cursor()
        try:
            if mode == "replace":
                cur.execute(f"TRUNCATE TABLE {table}")
            placeholders = ", ".join(["%s"] * len(columns))
            col_list = ", ".join(columns)
            insert = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
            rows = [tuple(row) for row in df.itertuples(index=False, name=None)]
            for start in range(0, len(rows), _INSERT_BATCH):
                for row in rows[start : start + _INSERT_BATCH]:
                    cur.execute(insert, row)
        finally:
            cur.close()
        return WriteResult(table=table, rows_written=len(df), mode=mode)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _oauth_m2m_provider(settings: dict[str, Any]) -> Any:
    core = optional_module("databricks.sdk.core", "databricks")
    config = core.Config(
        host=f"https://{settings['server_hostname']}",
        client_id=settings["client_id"],
        client_secret=settings["client_secret"],
    )
    return core.oauth_service_principal(config)
