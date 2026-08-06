"""Snowflake data source.

Auth: key-pair (recommended) via ``private_key_path``/``private_key`` + ``passphrase``, or
username/password. Store the private key or password in Secret Manager and inject it as an env
var through ``secrets=`` on your deployed function.
"""

from __future__ import annotations

from typing import Any

from rebase._optional import optional_module
from rebase.sources.base import ConnectorSpec, DataSource, DataSourceError, Frame, WriteResult

_SPEC = ConnectorSpec(
    provider="snowflake",
    fields={
        "account": ("SNOWFLAKE_ACCOUNT",),
        "user": ("SNOWFLAKE_USER",),
        "password": ("SNOWFLAKE_PASSWORD",),
        "private_key": ("SNOWFLAKE_PRIVATE_KEY",),
        "private_key_path": ("SNOWFLAKE_PRIVATE_KEY_PATH",),
        "passphrase": ("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE",),
        "role": ("SNOWFLAKE_ROLE",),
        "warehouse": ("SNOWFLAKE_WAREHOUSE",),
        "database": ("SNOWFLAKE_DATABASE",),
        "schema": ("SNOWFLAKE_SCHEMA",),
    },
    required=("account", "user"),
)


class SnowflakeSource(DataSource):
    provider = "snowflake"

    def __init__(self, *, connection: str | None = None, **overrides: Any) -> None:
        settings = _SPEC.resolve(connection, overrides)
        super().__init__(connection=connection, settings=settings)
        self._conn: Any | None = None

    def _connect_kwargs(self) -> dict[str, Any]:
        s = self.settings
        kwargs: dict[str, Any] = {"account": s["account"], "user": s["user"]}
        for optional in ("role", "warehouse", "database", "schema"):
            if s.get(optional):
                kwargs[optional] = s[optional]

        private_key = _load_private_key(s)
        if private_key is not None:
            kwargs["private_key"] = private_key
        elif s.get("password"):
            kwargs["password"] = s["password"]
        else:
            raise DataSourceError("snowflake: provide key-pair auth (private_key/private_key_path) or a password.")
        return kwargs

    def _get_conn(self) -> Any:
        if self._conn is None:
            connector = optional_module("snowflake.connector", "snowflake")
            self._conn = connector.connect(**self._connect_kwargs())
        return self._conn

    def _read_frame(self, query: str, params: Any | None = None) -> Frame:
        cur = self._get_conn().cursor()
        try:
            cur.execute(query, params)
            return cur.fetch_pandas_all()
        finally:
            cur.close()

    def _write(self, df: Frame, table: str, mode: str) -> WriteResult:
        pandas_tools = optional_module("snowflake.connector.pandas_tools", "snowflake")
        conn = self._get_conn()
        overwrite = mode == "replace"
        success, _chunks, nrows, _ = pandas_tools.write_pandas(
            conn, df, table, auto_create_table=True, overwrite=overwrite
        )
        if not success:
            raise DataSourceError(f"snowflake: write_pandas to {table!r} reported failure.")
        return WriteResult(table=table, rows_written=nrows, mode=mode)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _load_private_key(settings: dict[str, Any]) -> Any | None:
    pem: bytes | None = None
    if settings.get("private_key"):
        raw = settings["private_key"]
        pem = raw.encode() if isinstance(raw, str) else raw
    elif settings.get("private_key_path"):
        with open(settings["private_key_path"], "rb") as fh:
            pem = fh.read()
    if pem is None:
        return None

    serialization = optional_module("cryptography.hazmat.primitives.serialization", "snowflake")
    passphrase = settings.get("passphrase")
    key = serialization.load_pem_private_key(
        pem, password=passphrase.encode() if isinstance(passphrase, str) else passphrase
    )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
