"""Dataset registry, config diff/preflight, and the check/sync CLI commands."""

from __future__ import annotations

import warnings
from typing import Any

import pytest

import rebase as rb
from rebase.client import (
    Project,
    RebaseWorkflowError,
    Workflow,
    preflight_datasets,
    registered_datasets,
)


def _contract(maximum: float = 4000) -> rb.Contract:
    return rb.Contract(columns=[rb.Column("price", "float", between=(-500, maximum))])


class FakeClient:
    def __init__(self, stored: dict[str, dict[str, Any]] | None = None, *, fail: bool = False) -> None:
        self.stored = stored or {}
        self.fail = fail
        self.patched: list[tuple[str, dict[str, Any]]] = []

    def get_dataset(self, name: str) -> dict[str, Any]:
        if self.fail:
            raise RebaseWorkflowError("api down")
        if name not in self.stored:
            raise RebaseWorkflowError("dataset not found")
        return dict(self.stored[name])

    def update_dataset(self, name: str, **updates: Any) -> dict[str, Any]:
        self.patched.append((name, updates))
        self.stored.setdefault(name, {}).update(updates)
        return dict(self.stored[name])

    def create_dataset(self, name: str, description: str | None = None) -> dict[str, Any]:
        return self.stored.setdefault(name, {})


class TestRegistry:
    def test_only_configured_datasets_register(self) -> None:
        configured = rb.Dataset.from_name("a/b", contract=_contract())
        rb.Dataset.from_name("plain/pointer")
        assert registered_datasets() == [configured]

    def test_redefinition_with_different_config_warns_last_wins(self) -> None:
        rb.Dataset.from_name("a/b", contract=_contract(4000))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            newer = rb.Dataset.from_name("a/b", contract=_contract(5000))
        assert any("redefined" in str(w.message) for w in caught)
        assert registered_datasets() == [newer]

    def test_identical_redefinition_is_silent(self) -> None:
        rb.Dataset.from_name("a/b", contract=_contract())
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            rb.Dataset.from_name("a/b", contract=_contract())
        assert not caught


class TestConfigDiff:
    def test_reports_drift_and_new_keys(self) -> None:
        dataset = rb.Dataset.from_name("a/b", contract=_contract(5000), freshness=rb.Freshness("45m"))
        client = FakeClient({"a/b": {"contract": _contract(4000).to_dict(), "freshness": None}})
        diff = dataset.config_diff(client)
        assert set(diff) == {"contract", "freshness"}
        assert diff["contract"][0] == _contract(4000).to_dict()
        assert diff["freshness"] == (None, {"max_age": "45m"})

    def test_in_sync_returns_empty(self) -> None:
        dataset = rb.Dataset.from_name("a/b", contract=_contract())
        client = FakeClient({"a/b": {"contract": _contract().to_dict()}})
        assert dataset.config_diff(client) == {}

    def test_missing_dataset_raises_without_create_if_missing(self) -> None:
        dataset = rb.Dataset.from_name("a/b", contract=_contract())
        with pytest.raises(RebaseWorkflowError, match="not found"):
            dataset.config_diff(FakeClient())

    def test_missing_dataset_counts_as_new_with_create_if_missing(self) -> None:
        dataset = rb.Dataset.from_name("a/b", create_if_missing=True, contract=_contract())
        diff = dataset.config_diff(FakeClient())
        assert diff["contract"][0] is None


class TestPreflight:
    def test_publishes_first_publication(self) -> None:
        dataset = rb.Dataset.from_name("a/b", contract=_contract())
        client = FakeClient({"a/b": {"contract": None}})
        preflight_datasets(client, datasets=[dataset])
        assert client.patched == [("a/b", {"contract": _contract().to_dict()})]

    def test_fails_on_drift_with_diff_in_message(self) -> None:
        dataset = rb.Dataset.from_name("a/b", contract=_contract(5000))
        client = FakeClient({"a/b": {"contract": _contract(4000).to_dict()}})
        with pytest.raises(RebaseWorkflowError, match="drift") as exc_info:
            preflight_datasets(client, datasets=[dataset])
        message = str(exc_info.value)
        assert "a/b" in message and "maximum" in message and "rebase dataset sync" in message
        assert client.patched == []

    def test_fails_on_fetch_error(self) -> None:
        dataset = rb.Dataset.from_name("a/b", contract=_contract())
        with pytest.raises(RebaseWorkflowError, match="could not verify"):
            preflight_datasets(FakeClient(fail=True), datasets=[dataset])

    def test_uses_registry_by_default(self) -> None:
        rb.Dataset.from_name("a/b", contract=_contract())
        client = FakeClient({"a/b": {"contract": _contract().to_dict()}})
        preflight_datasets(client)  # in-sync: no error, no patch
        assert client.patched == []


class TestRuntimeSyncNoOverwrite:
    def test_first_use_publishes_when_absent(self) -> None:
        dataset = rb.Dataset.from_name("a/b", contract=_contract())
        client = FakeClient({"a/b": {"contract": None}})
        dataset._sync_config(client)
        assert client.patched == [("a/b", {"contract": _contract().to_dict()})]

    def test_drift_warns_and_does_not_overwrite(self) -> None:
        dataset = rb.Dataset.from_name("a/b", contract=_contract(5000))
        client = FakeClient({"a/b": {"contract": _contract(4000).to_dict()}})
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            dataset._sync_config(client)
        assert any("rebase dataset sync" in str(w.message) for w in caught)
        assert client.patched == []


class TestDeployHooks:
    def test_project_deploy_runs_preflight_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[Any] = []
        monkeypatch.setattr("rebase.client.preflight_datasets", lambda client=None, **kw: calls.append(client))
        project = rb.Project("energy")

        @project.workflow()
        def wf_one():
            return {}

        @project.workflow()
        def wf_two():
            return {}

        monkeypatch.setattr(Project, "_client", property(lambda self: FakeClient()), raising=False)
        monkeypatch.setattr(FakeClient, "ensure_project", lambda self, name, **kw: {"id": "p"}, raising=False)
        from rebase.deployment import _PreparedDefinition

        def prepare(workflow, **kw):
            calls.append(("wf", kw.get("_skip_dataset_preflight")))
            return _PreparedDefinition("POST", "/workflows", {}, "workflow")

        monkeypatch.setattr(Workflow, "_prepare_deployment", prepare)
        monkeypatch.setattr(Workflow, "deploy", lambda self, **kw: self)
        monkeypatch.setattr(FakeClient, "_compare_definitions", lambda *args: {}, raising=False)
        project.deploy()
        preflight_calls = [c for c in calls if not isinstance(c, tuple)]
        workflow_calls = [c for c in calls if isinstance(c, tuple)]
        assert len(preflight_calls) == 1
        assert workflow_calls == [("wf", True), ("wf", True)]

    def test_standalone_workflow_deploy_runs_preflight_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _PreflightRan(Exception):
            pass

        def fake_preflight(client=None, **kw: Any) -> None:
            raise _PreflightRan

        monkeypatch.setattr("rebase.client.preflight_datasets", fake_preflight)

        @rb.workflow(project="energy")
        def wf():
            return {}

        # The preflight fires before any step deploy or registration call.
        with pytest.raises(_PreflightRan):
            wf.deploy()


def _write_datasets_file(tmp_path, maximum: float = 4000) -> str:
    path = tmp_path / "datasets_decl.py"
    path.write_text(
        "import rebase as rb\n"
        "prices = rb.Dataset.from_name(\n"
        "    'nordpool/prices',\n"
        f"    contract=rb.Contract(columns=[rb.Column('price', 'float', between=(-500, {maximum}))]),\n"
        ")\n"
        "weather = rb.Dataset.from_name('weather/ecmwf', freshness=rb.Freshness('7h'))\n"
    )
    return str(path)


class TestCliCheckAndSync:
    def _fake_client(self, monkeypatch: pytest.MonkeyPatch, stored: dict[str, dict[str, Any]]) -> FakeClient:
        fake = FakeClient(stored)
        monkeypatch.setattr("rebase.cli.Client", lambda *a, **kw: fake)
        return fake

    def test_check_exits_zero_when_in_sync_or_new(self, tmp_path, monkeypatch, capsys) -> None:
        from rebase.cli import main

        self._fake_client(
            monkeypatch,
            {
                "nordpool/prices": {"contract": _contract().to_dict()},
                "weather/ecmwf": {"freshness": None},
            },
        )
        assert main(["dataset", "check", _write_datasets_file(tmp_path)]) == 0
        output = capsys.readouterr().out
        assert "in-sync" in output and "new" in output

    def test_check_exits_one_on_drift(self, tmp_path, monkeypatch, capsys) -> None:
        from rebase.cli import main

        self._fake_client(
            monkeypatch,
            {
                "nordpool/prices": {"contract": _contract(4000).to_dict()},
                "weather/ecmwf": {"freshness": None},
            },
        )
        assert main(["dataset", "check", _write_datasets_file(tmp_path, maximum=5000)]) == 1
        output = capsys.readouterr().out
        assert "drift" in output and "rebase dataset sync" in output

    def test_sync_applies_drifted_and_new(self, tmp_path, monkeypatch, capsys) -> None:
        from rebase.cli import main

        fake = self._fake_client(
            monkeypatch,
            {
                "nordpool/prices": {"contract": _contract(4000).to_dict()},
                "weather/ecmwf": {"freshness": None},
            },
        )
        assert main(["dataset", "sync", _write_datasets_file(tmp_path, maximum=5000), "--yes"]) == 0
        patched_names = sorted(name for name, _ in fake.patched)
        assert patched_names == ["nordpool/prices", "weather/ecmwf"]
        assert "Applied" in capsys.readouterr().out

    def test_sync_without_confirmation_aborts(self, tmp_path, monkeypatch) -> None:
        from rebase.cli import main

        fake = self._fake_client(
            monkeypatch,
            {
                "nordpool/prices": {"contract": _contract(4000).to_dict()},
                "weather/ecmwf": {"freshness": None},
            },
        )
        monkeypatch.setattr("typer.confirm", lambda *a, **kw: False)
        assert main(["dataset", "sync", _write_datasets_file(tmp_path, maximum=5000)]) == 1
        assert fake.patched == []

    def test_check_rejects_file_without_datasets(self, tmp_path, monkeypatch) -> None:
        from rebase.cli import main

        path = tmp_path / "empty_decl.py"
        path.write_text("x = 1\n")
        assert main(["dataset", "check", str(path)]) == 1
