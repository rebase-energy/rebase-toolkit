import importlib.util
import warnings
from datetime import UTC, datetime, timedelta

import pytest

import rebase as rb
from rebase.sources.base import (
    BitemporalSpec,
    ConnectorSpec,
    DataSource,
    DataSourceError,
    KnowledgeTime,
    SignalOutcome,
    WriteResult,
    _replay_knowledge_time,
    _resolve_now,
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


class _RecordingDataset:
    def __init__(self, name: str = "nordpool/prices", *, contract: dict | None = None) -> None:
        self.name = name
        self.contract = contract
        self.freshness = None
        self.signals: list[dict] = []

    def mark_updated(self, watermark=None, *, validation=None, source="sdk", run_id=None) -> dict:
        self.signals.append({"watermark": watermark, "validation": validation, "source": source, "run_id": run_id})
        return {"dataset": self.name, "fired": ["run-1"]}


class _FakeSource(DataSource):
    provider = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.writes = 0

    def _read_frame(self, query, params=None):
        raise NotImplementedError

    def _write(self, df, table, mode):
        if self.fail:
            raise DataSourceError("fake write failed")
        self.writes += 1
        return WriteResult(table=table, rows_written=2, mode=mode)


def test_write_signals_dataset_on_success() -> None:
    dataset = _RecordingDataset()

    result = _FakeSource().write(object(), "prices", dataset=dataset)

    assert (result.table, result.rows_written, result.mode) == ("prices", 2, "append")
    assert result.signal == SignalOutcome(sent=True, fired=["run-1"])
    assert result.validation is None  # no contract anywhere -> validation skipped entirely
    assert len(dataset.signals) == 1
    assert dataset.signals[0]["source"] == "source_write"


def test_write_resolves_dataset_names(monkeypatch) -> None:
    from rebase.client import Dataset

    observed: dict[str, str] = {}

    def fake_mark_updated(self, watermark=None, *, validation=None, source="sdk", run_id=None):
        observed.setdefault("name", self.name)
        return {"fired": []}

    monkeypatch.setattr(Dataset, "mark_updated", fake_mark_updated)
    monkeypatch.setattr(Dataset, "_stored_contract", lambda self, **kwargs: None)

    _FakeSource().write(object(), "prices", dataset="nordpool/prices")

    assert observed == {"name": "nordpool/prices"}


def test_write_signal_failure_does_not_fail_write() -> None:
    class _ExplodingDataset:
        name = "nordpool/prices"
        contract = None

        def mark_updated(self, watermark=None, *, validation=None, source="sdk", run_id=None):
            raise RuntimeError("api down")

    with pytest.warns(UserWarning, match="dataset signal after writing"):
        result = _FakeSource().write(object(), "prices", dataset=_ExplodingDataset())

    assert (result.table, result.rows_written, result.mode) == ("prices", 2, "append")
    assert result.signal.sent is False
    assert "api down" in result.signal.error


def test_write_does_not_signal_without_dataset(monkeypatch) -> None:
    from rebase.client import Dataset

    def fail_mark_updated(self, watermark=None, **kwargs):
        raise AssertionError("write without dataset= must not signal")

    monkeypatch.setattr(Dataset, "mark_updated", fail_mark_updated)

    result = _FakeSource().write(object(), "prices", mode="replace")

    assert result == WriteResult(table="prices", rows_written=2, mode="replace")
    assert result.signal is None
    assert result.validation is None


def test_write_failure_does_not_signal() -> None:
    dataset = _RecordingDataset()

    with pytest.raises(DataSourceError, match="fake write failed"):
        _FakeSource(fail=True).write(object(), "prices", dataset=dataset)

    assert dataset.signals == []


# --- contract-aware write pipeline ----------------------------------------------------


def _contract_dict(**overrides):
    import rebase as rb

    kwargs = {
        "primary_key": ("delivery_start", "area"),
        "min_rows": 1,
        "watermark_column": "delivery_start",
        **overrides,
    }
    contract = rb.Contract(
        [
            rb.Column("price", "float", not_null=True, between=(-500, 4000)),
            rb.Column("area", "string", not_null=True, isin=["SE1", "SE2"]),
            rb.Column("delivery_start", "timestamp", not_null=True),
        ],
        **kwargs,
    )
    return contract.to_dict()


def _good_frame():
    import pandas as pd

    return pd.DataFrame(
        {
            "price": [10.0, 20.0],
            "area": ["SE1", "SE2"],
            "delivery_start": pd.to_datetime(["2026-07-11T09:00:00Z", "2026-07-11T10:00:00Z"], utc=True),
        }
    )


def _bad_frame():
    frame = _good_frame()
    frame.loc[1, "price"] = 9999.0
    return frame


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_write_contract_failure_blocks_write() -> None:
    from rebase.contract import ContractViolation

    source = _FakeSource()
    dataset = _RecordingDataset(contract=_contract_dict())

    with pytest.raises(ContractViolation, match="Nothing was written"):
        source.write(_bad_frame(), "prices", dataset=dataset)

    assert source.writes == 0
    assert dataset.signals == []


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_write_contract_warn_writes_and_flags_signal(caplog) -> None:
    import logging

    source = _FakeSource()
    dataset = _RecordingDataset(contract=_contract_dict(on_violation="warn"))

    with caplog.at_level(logging.WARNING, logger="rebase.sources"):
        result = source.write(_bad_frame(), "prices", dataset=dataset)

    assert source.writes == 1
    assert any("writing anyway" in record.message for record in caplog.records)
    assert result.validation is not None and result.validation.passed is False
    assert dataset.signals[0]["validation"]["passed"] is False
    assert dataset.signals[0]["validation"]["failures"][0]["check"] == "range"


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_write_on_violation_argument_overrides_contract() -> None:
    source = _FakeSource()
    dataset = _RecordingDataset(contract=_contract_dict())  # contract says fail

    result = source.write(_bad_frame(), "prices", dataset=dataset, on_violation="warn")

    assert source.writes == 1
    assert result.signal.sent is True


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_write_derives_watermark_from_contract_column() -> None:
    dataset = _RecordingDataset(contract=_contract_dict())

    result = _FakeSource().write(_good_frame(), "prices", dataset=dataset)

    assert result.watermark == "2026-07-11T10:00:00+00:00"
    assert dataset.signals[0]["watermark"] == "2026-07-11T10:00:00+00:00"
    assert result.validation.passed is True
    assert dataset.signals[0]["validation"]["passed"] is True


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_write_explicit_watermark_wins_over_watermark_column() -> None:
    dataset = _RecordingDataset(contract=_contract_dict())

    result = _FakeSource().write(_good_frame(), "prices", dataset=dataset, watermark="manual-w")

    assert result.watermark == "manual-w"
    assert dataset.signals[0]["watermark"] == "manual-w"


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_write_explicit_none_watermark_sends_null() -> None:
    dataset = _RecordingDataset(contract=_contract_dict())

    result = _FakeSource().write(_good_frame(), "prices", dataset=dataset, watermark=None)

    assert result.watermark is None
    assert dataset.signals[0]["watermark"] is None


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_write_validate_false_sends_skipped_payload() -> None:
    dataset = _RecordingDataset(contract=_contract_dict())

    result = _FakeSource().write(_bad_frame(), "prices", dataset=dataset, validate=False)

    assert result.validation.skipped is True
    payload = dataset.signals[0]["validation"]
    assert payload == {"passed": False, "checks": 0, "row_count": 2, "failures": [], "skipped": True}


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_write_fetches_stored_contract_when_no_in_code(monkeypatch) -> None:
    from rebase.client import Client, Dataset
    from rebase.contract import ContractViolation

    stored = _contract_dict()
    calls = {"get": 0}

    def fake_get_dataset(self, name):
        calls["get"] += 1
        return {"name": name, "contract": stored}

    monkeypatch.setattr(Client, "get_dataset", fake_get_dataset)
    dataset = Dataset("nordpool/prices", client=Client(api_key="rbw_test", api_url="https://api.example.com"))

    with pytest.raises(ContractViolation):
        _FakeSource().write(_bad_frame(), "prices", dataset=dataset)

    assert calls["get"] == 1
    # a second write reuses the cached stored contract
    with pytest.raises(ContractViolation):
        _FakeSource().write(_bad_frame(), "prices", dataset=dataset)
    assert calls["get"] == 1


# --- replay-aware sources --------------------------------------------------------------


_REPLAY_BOUND = "2026-07-10T09:00:00+00:00"


class _FrameSource(_FakeSource):
    """A fake source whose reads return a fixed pandas frame."""

    def __init__(self, frame) -> None:
        super().__init__()
        self.frame = frame

    def _read_frame(self, query, params=None):
        return self.frame


def test_replay_knowledge_time_parses_env(monkeypatch) -> None:
    monkeypatch.delenv("REBASE_REPLAY_KNOWLEDGE_TIME", raising=False)
    assert _replay_knowledge_time() is None

    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", _REPLAY_BOUND)
    assert _replay_knowledge_time() == datetime(2026, 7, 10, 9, tzinfo=UTC)

    # naive datetimes are assumed UTC
    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", "2026-07-10T09:00:00")
    assert _replay_knowledge_time() == datetime(2026, 7, 10, 9, tzinfo=UTC)


def test_replay_knowledge_time_warns_on_garbage(monkeypatch) -> None:
    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", "not-a-datetime")
    with pytest.warns(UserWarning, match="REBASE_REPLAY_KNOWLEDGE_TIME"):
        assert _replay_knowledge_time() is None


def test_resolve_now_returns_replay_bound_under_env(monkeypatch) -> None:
    monkeypatch.delenv("REBASE_REPLAY_KNOWLEDGE_TIME", raising=False)
    assert abs((_resolve_now() - datetime.now(UTC)).total_seconds()) < 5

    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", _REPLAY_BOUND)
    assert _resolve_now() == datetime(2026, 7, 10, 9, tzinfo=UTC)


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_read_bitemporal_filters_rows_after_replay_bound(monkeypatch) -> None:
    import pandas as pd

    frame = pd.DataFrame(
        {
            "ts": ["2026-07-10T08:00:00Z", "2026-07-10T10:00:00Z"],
            "issued": ["2026-07-10T08:30:00Z", "2026-07-10T10:30:00Z"],  # second is after the bound
            "v": [1.0, 2.0],
        }
    )
    spec = BitemporalSpec(valid_time="ts", knowledge_time="issued")

    monkeypatch.delenv("REBASE_REPLAY_KNOWLEDGE_TIME", raising=False)
    assert len(_FrameSource(frame.copy()).read_bitemporal("SELECT 1", spec)) == 2

    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", _REPLAY_BOUND)
    out = _FrameSource(frame.copy()).read_bitemporal("SELECT 1", spec)
    assert list(out["v"]) == [1.0]


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_read_bitemporal_ingestion_fallback_keeps_rows_during_replay(monkeypatch) -> None:
    import pandas as pd

    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", _REPLAY_BOUND)
    frame = pd.DataFrame({"ts": ["2026-07-10T08:00:00Z"], "v": [1.0]})

    with pytest.warns(UserWarning, match="may leak future information"):
        out = _FrameSource(frame).read_bitemporal("SELECT 1", BitemporalSpec(valid_time="ts"))

    # knowledge_time is stamped at the replay bound (not wall-clock now), so the row survives the filter
    assert len(out) == 1
    assert out["knowledge_time"].iloc[0] == pd.Timestamp(_REPLAY_BOUND)


def test_write_during_replay_suppresses_signal_but_still_writes(monkeypatch, caplog) -> None:
    import logging

    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", _REPLAY_BOUND)
    dataset = _RecordingDataset()
    source = _FakeSource()

    with caplog.at_level(logging.WARNING, logger="rebase.sources"):
        result = source.write(object(), "prices", dataset=dataset)

    assert source.writes == 1
    assert dataset.signals == []
    assert result.signal.sent is False
    assert "replay" in result.signal.error
    assert any("ctx.is_replay" in record.message for record in caplog.records)


def test_write_without_dataset_warns_during_replay(monkeypatch, caplog) -> None:
    import logging

    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", _REPLAY_BOUND)
    source = _FakeSource()

    with caplog.at_level(logging.WARNING, logger="rebase.sources"):
        result = source.write(object(), "prices")

    assert source.writes == 1
    assert result.signal is None
    assert any("ctx.is_replay" in record.message for record in caplog.records)


def test_mark_updated_suppressed_during_replay(monkeypatch, caplog) -> None:
    import logging

    from rebase.client import Client, Dataset

    def fail_signal(self, name, **kwargs):
        raise AssertionError("replay must not signal datasets")

    monkeypatch.setattr(Client, "signal_dataset", fail_signal)
    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", _REPLAY_BOUND)
    dataset = Dataset("nordpool/prices", client=Client(api_key="rbw_test", api_url="https://api.example.com"))

    with caplog.at_level(logging.WARNING, logger="rebase.client"):
        response = dataset.mark_updated(watermark="2026-07-10T09:00:00Z")

    assert response == {"suppressed": "replay", "dataset": "nordpool/prices"}
    assert any("suppressing dataset signal" in record.message for record in caplog.records)


def test_series_values_select_defaults_as_of_to_replay_bound(monkeypatch) -> None:
    from rebase.sources.energy import series_values_select

    monkeypatch.delenv("REBASE_REPLAY_KNOWLEDGE_TIME", raising=False)
    sql, params = series_values_select("t", [1])
    assert "as_of" not in params

    monkeypatch.setenv("REBASE_REPLAY_KNOWLEDGE_TIME", _REPLAY_BOUND)
    sql, params = series_values_select("t", [1])
    assert "knowledge_time <= @as_of" in sql
    assert params["as_of"] == datetime(2026, 7, 10, 9, tzinfo=UTC)

    # an explicit as_of always wins
    explicit = datetime(2026, 7, 1, tzinfo=UTC)
    _, params = series_values_select("t", [1], as_of=explicit)
    assert params["as_of"] == explicit


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_write_warns_on_drift_and_never_overwrites_stored_config(monkeypatch) -> None:
    from rebase.client import Client, Dataset

    stored_contract = _contract_dict(min_rows=None)  # differs from the in-code contract
    patches: list[dict] = []
    signals: list[dict] = []

    monkeypatch.setattr(Client, "get_dataset", lambda self, name: {"name": name, "contract": stored_contract})

    def fake_update_dataset(self, name, **kwargs):
        patches.append(kwargs)
        return {"name": name, **kwargs}

    def fake_signal_dataset(self, name, **kwargs):
        signals.append(kwargs)
        return {"dataset": name, "fired": []}

    monkeypatch.setattr(Client, "update_dataset", fake_update_dataset)
    monkeypatch.setattr(Client, "signal_dataset", fake_signal_dataset)

    client = Client(api_key="rbw_test", api_url="https://api.example.com")
    dataset = Dataset("nordpool/prices", client=client, contract=_contract_dict())

    source = _FakeSource()
    # Runtime drift warns (pointing at `rebase dataset sync`) but never PATCHes:
    # changing a published config is deliberate. Warned once per instance.
    with pytest.warns(UserWarning, match="rebase dataset sync"):
        source.write(_good_frame(), "prices", dataset=dataset)
    source.write(_good_frame(), "prices", dataset=dataset)

    assert patches == []
    assert len(signals) == 2
    assert signals[0]["source"] == "source_write"


# --- declared knowledge time ---------------------------------------------------------


def test_knowledge_time_constructors_validate() -> None:
    with pytest.raises(DataSourceError, match="non-empty column"):
        KnowledgeTime.from_source("")
    with pytest.raises(DataSourceError, match="at least one input"):
        KnowledgeTime.from_inputs()
    with pytest.raises(DataSourceError, match="requires a datetime"):
        KnowledgeTime.at("2026-01-01")


def test_knowledge_time_at_warns_on_naive_datetime() -> None:
    with pytest.warns(UserWarning, match="timezone-naive"):
        KnowledgeTime.at(datetime(2026, 1, 1, 9, 0))


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_knowledge_time_from_source_stamps_the_column() -> None:
    import pandas as pd

    df = pd.DataFrame(
        {
            "valid_time": pd.to_datetime(["2026-01-01T00:00Z", "2026-01-01T01:00Z"]),
            "issued_at": pd.to_datetime(["2026-01-01T00:05Z", "2026-01-01T01:05Z"]),
            "value": [1.0, 2.0],
        }
    )
    out = KnowledgeTime.from_source("issued_at").apply(df)
    assert list(out["knowledge_time"]) == list(pd.to_datetime(["2026-01-01T00:05Z", "2026-01-01T01:05Z"]))
    assert "knowledge_time" not in df.columns  # the caller's frame is untouched


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_knowledge_time_from_source_rejects_missing_column_and_nulls() -> None:
    import pandas as pd

    with pytest.raises(DataSourceError, match="not found in frame columns"):
        KnowledgeTime.from_source("issued_at").apply(pd.DataFrame({"value": [1.0]}))
    df = pd.DataFrame({"issued_at": pd.to_datetime(["2026-01-01T00:05Z", None]), "value": [1.0, 2.0]})
    with pytest.raises(DataSourceError, match="1 rows have no publication time"):
        KnowledgeTime.from_source("issued_at").apply(df)


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_knowledge_time_from_source_warns_on_naive_column() -> None:
    import pandas as pd

    df = pd.DataFrame({"issued_at": pd.to_datetime(["2026-01-01T00:05", "2026-01-01T01:05"]), "value": [1.0, 2.0]})
    with pytest.warns(UserWarning, match="timezone-naive"):
        out = KnowledgeTime.from_source("issued_at").apply(df)
    assert list(out["knowledge_time"]) == list(pd.to_datetime(["2026-01-01T00:05Z", "2026-01-01T01:05Z"]))


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_knowledge_time_from_source_does_not_warn_on_tz_aware_column() -> None:
    import pandas as pd

    df = pd.DataFrame({"issued_at": pd.to_datetime(["2026-01-01T00:05Z", "2026-01-01T01:05Z"]), "value": [1.0, 2.0]})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        out = KnowledgeTime.from_source("issued_at").apply(df)
    assert list(out["knowledge_time"]) == list(pd.to_datetime(["2026-01-01T00:05Z", "2026-01-01T01:05Z"]))


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_knowledge_time_from_inputs_takes_the_max() -> None:
    import pandas as pd

    actuals = pd.DataFrame({"knowledge_time": pd.to_datetime(["2026-01-01T00:00Z", "2026-01-01T06:00Z"])})
    weather = pd.DataFrame({"knowledge_time": pd.to_datetime(["2026-01-01T03:00Z"])})
    out = KnowledgeTime.from_inputs(actuals, weather).apply(pd.DataFrame({"value": [1.0, 2.0]}))
    assert set(out["knowledge_time"]) == {pd.Timestamp("2026-01-01T06:00Z")}
    assert len(out) == 2


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_knowledge_time_from_inputs_names_the_offending_input() -> None:
    import pandas as pd

    good = pd.DataFrame({"knowledge_time": pd.to_datetime(["2026-01-01T00:00Z"])})
    bad = pd.DataFrame({"value": [1.0]})
    with pytest.raises(DataSourceError, match="input 1 has no knowledge_time"):
        KnowledgeTime.from_inputs(good, bad).apply(pd.DataFrame({"value": [1.0]}))


@pytest.mark.skipif(not _HAS_PANDAS, reason="pandas not installed in this environment")
def test_knowledge_time_at_stamps_a_scalar() -> None:
    import pandas as pd

    moment = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    out = KnowledgeTime.at(moment).apply(pd.DataFrame({"value": [1.0, 2.0]}))
    assert set(out["knowledge_time"]) == {pd.Timestamp(moment)}


def test_knowledge_time_is_exported() -> None:
    assert rb.sources.KnowledgeTime is KnowledgeTime
