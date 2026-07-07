import importlib.util
from datetime import timedelta

import pytest

import rebase as rb
from rebase.sources.base import (
    BitemporalSpec,
    ConnectorSpec,
    DataSourceError,
    apply_bitemporal,
    resolve_settings,
)

_HAS_PANDAS = importlib.util.find_spec("pandas") is not None


def test_factories_are_exported() -> None:
    for name in ("snowflake", "databricks", "bigquery", "fabric"):
        assert callable(getattr(rb.sources, name))
    assert rb.sources is rb.sources  # importing rebase does not import any warehouse driver


def test_resolve_settings_precedence(monkeypatch) -> None:
    monkeypatch.setenv("REBASE_SOURCE_ACME_ACCOUNT", "from-connection")
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "from-standard-env")

    fields = {"account": ("SNOWFLAKE_ACCOUNT",), "user": ("SNOWFLAKE_USER",)}

    # explicit override wins over everything
    assert resolve_settings("acme", fields, {"account": "explicit"})["account"] == "explicit"
    # connection-scoped env wins over provider-standard env
    assert resolve_settings("acme", fields, {})["account"] == "from-connection"
    # provider-standard env is the final fallback
    assert resolve_settings(None, fields, {})["account"] == "from-standard-env"
    # unset fields are simply absent
    assert "user" not in resolve_settings("acme", fields, {})


def test_connector_spec_reports_missing_required() -> None:
    spec = ConnectorSpec(provider="snowflake", fields={"account": ()}, required=("account",))
    with pytest.raises(DataSourceError, match="missing required setting"):
        spec.resolve("acme", {})


def test_bitemporal_spec_rejects_both_knowledge_inputs() -> None:
    with pytest.raises(ValueError, match="at most one"):
        BitemporalSpec(valid_time="ts", knowledge_time="issued", knowledge_delay=timedelta(hours=1))


def test_missing_driver_raises_helpful_error() -> None:
    # Factory construction is lazy; the connection is only opened on first read.
    source = rb.sources.snowflake(account="acct", user="usr", password="pw")
    with pytest.raises(DataSourceError, match=r"snowflake read failed"):
        source.read("SELECT 1")


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_apply_bitemporal_with_knowledge_column() -> None:
    import pandas as pd

    df = pd.DataFrame({"ts": ["2024-01-01T00:00:00Z"], "issued": ["2024-01-01T01:00:00Z"], "v": [3.0]})
    out = apply_bitemporal(df, BitemporalSpec(valid_time="ts", knowledge_time="issued"))
    assert "valid_time" in out.columns
    assert "knowledge_time" in out.columns
    assert out["knowledge_time"].iloc[0] > out["valid_time"].iloc[0]


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_apply_bitemporal_with_delay() -> None:
    import pandas as pd

    df = pd.DataFrame({"ts": ["2024-01-01T00:00:00Z"], "v": [3.0]})
    out = apply_bitemporal(df, BitemporalSpec(valid_time="ts", knowledge_delay=timedelta(hours=2)))
    delta = out["knowledge_time"].iloc[0] - out["valid_time"].iloc[0]
    assert delta == timedelta(hours=2)


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_apply_bitemporal_warns_without_knowledge_time() -> None:
    import pandas as pd

    df = pd.DataFrame({"ts": ["2024-01-01T00:00:00Z"], "v": [3.0]})
    with pytest.warns(UserWarning, match="may leak future information"):
        out = apply_bitemporal(df, BitemporalSpec(valid_time="ts"))
    assert "knowledge_time" in out.columns
