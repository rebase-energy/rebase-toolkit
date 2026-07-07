"""Microsoft Fabric data source (SQL analytics endpoint).

Auth: Azure AD service principal (``client_id`` + ``client_secret`` + ``tenant_id``) against a
Fabric warehouse / lakehouse SQL analytics endpoint over ODBC. Requires the Microsoft ODBC
Driver 18 for SQL Server to be present in the deployed image.

For bulk ingest from OneLake you can instead read the Delta tables directly from ADLS Gen2;
this connector targets the T-SQL endpoint, which is the simplest first path.
"""

from __future__ import annotations

from typing import Any

from rebase._optional import optional_module
from rebase.sources.base import ConnectorSpec, DataSource, DataSourceError, Frame, WriteResult

_SPEC = ConnectorSpec(
    provider="fabric",
    fields={
        "server": ("FABRIC_SQL_ENDPOINT", "FABRIC_SERVER"),
        "database": ("FABRIC_DATABASE",),
        "client_id": ("FABRIC_CLIENT_ID", "AZURE_CLIENT_ID"),
        "client_secret": ("FABRIC_CLIENT_SECRET", "AZURE_CLIENT_SECRET"),
        "tenant_id": ("FABRIC_TENANT_ID", "AZURE_TENANT_ID"),
        "driver": ("FABRIC_ODBC_DRIVER",),
    },
    required=("server", "database", "client_id", "client_secret"),
)

_DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"
_INSERT_BATCH = 1000


class FabricSource(DataSource):
    provider = "fabric"

    def __init__(self, *, connection: str | None = None, **overrides: Any) -> None:
        settings = _SPEC.resolve(connection, overrides)
        super().__init__(connection=connection, settings=settings)
        self._conn: Any | None = None

    def _connection_string(self) -> str:
        s = self.settings
        driver = s.get("driver") or _DEFAULT_DRIVER
        parts = [
            f"Driver={{{driver}}}",
            f"Server={s['server']}",
            f"Database={s['database']}",
            "Encrypt=yes",
            "TrustServerCertificate=no",
            "Authentication=ActiveDirectoryServicePrincipal",
            f"UID={s['client_id']}",
            f"PWD={s['client_secret']}",
        ]
        if s.get("tenant_id"):
            parts.append(f"Authority Id={s['tenant_id']}")
        return ";".join(parts) + ";"

    def _get_conn(self) -> Any:
        if self._conn is None:
            pyodbc = optional_module("pyodbc", "fabric")
            self._conn = pyodbc.connect(self._connection_string())
        return self._conn

    def _read_frame(self, query: str, params: Any | None = None) -> Frame:
        pd = optional_module("pandas", "fabric")
        conn = self._get_conn()
        if params:
            return pd.read_sql(query, conn, params=params)
        return pd.read_sql(query, conn)

    def write(self, df: Frame, table: str, *, mode: str = "append") -> WriteResult:
        columns = list(df.columns)
        conn = self._get_conn()
        cur = conn.cursor()
        try:
            if mode == "replace":
                cur.execute(f"TRUNCATE TABLE {table}")
            placeholders = ", ".join(["?"] * len(columns))
            col_list = ", ".join(columns)
            insert = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
            cur.fast_executemany = True
            rows = [tuple(row) for row in df.itertuples(index=False, name=None)]
            for start in range(0, len(rows), _INSERT_BATCH):
                cur.executemany(insert, rows[start : start + _INSERT_BATCH])
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - surface as a uniform error after rollback
            conn.rollback()
            raise DataSourceError(f"fabric write to {table!r} failed: {exc}") from exc
        finally:
            cur.close()
        return WriteResult(table=table, rows_written=len(df), mode=mode)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
