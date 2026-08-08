"""Project-attached functions: secrets support, and no silent drops on deploy.

Two related gaps this covers. ``Project.function`` did not accept
``secrets``/``env``/``volumes`` even though ``Project.asgi_app`` and the
module-level ``rb.function`` did, which pushed anyone needing a secret onto
``rb.function(project=...)``. And ``deploy_file`` deploys *only* what is
attached to a top-level Project, so those standalone objects were skipped
without a word -- leaving a deployed workflow calling a function that was
never registered.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebase as rb
from rebase.cli import deploy_file
from rebase.client import RebaseWorkflowError


class TestProjectFunctionSecrets:
    def test_secrets_reach_the_function(self) -> None:
        project = rb.project("acme")

        @project.function(name="load", secrets=["acme-creds"])
        def load() -> dict:
            return {}

        assert load.secrets == ["acme-creds"]

    def test_env_and_volumes_reach_the_function(self) -> None:
        project = rb.project("acme")

        @project.function(name="load", env={"REGION": "eu"}, volumes={"/data": "archive"})
        def load() -> dict:
            return {}

        assert load.env == {"REGION": "eu"}
        assert load.volumes == {"/data": "archive"}

    def test_defaults_stay_unset(self) -> None:
        project = rb.project("acme")

        @project.function(name="load")
        def load() -> dict:
            return {}

        assert not load.secrets
        assert not load.env

    def test_function_is_attached_to_the_project(self) -> None:
        project = rb.project("acme")

        @project.function(name="load", secrets=["acme-creds"])
        def load() -> dict:
            return {}

        assert load in project._functions


PROJECT_WITH_ORPHAN = """
import rebase as rb

project = rb.project("acme")


@rb.function(project="acme", name="fetch", secrets=["acme-creds"])
def fetch() -> dict:
    return {}


@project.workflow(name="nightly")
def nightly() -> dict:
    return fetch()
"""

PROJECT_ALL_ATTACHED = """
import rebase as rb

project = rb.project("acme")


@project.function(name="fetch")
def fetch() -> dict:
    return {}
"""


class TestDeployRejectsOrphans:
    def _write(self, tmp_path: Path, source: str) -> Path:
        path = tmp_path / "targets.py"
        path.write_text(source)
        return path

    def test_standalone_function_is_reported_not_skipped(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, PROJECT_WITH_ORPHAN)

        with pytest.raises(RebaseWorkflowError) as excinfo:
            deploy_file(path)

        message = str(excinfo.value)
        assert "fetch" in message
        assert "not attached to a project" in message

    def test_error_names_the_decorators_to_use(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, PROJECT_WITH_ORPHAN)

        with pytest.raises(RebaseWorkflowError, match=r"@project\.function"):
            deploy_file(path)

    def test_fully_attached_file_gets_past_the_guard(self, tmp_path: Path, monkeypatch) -> None:
        """The guard must not fire when everything belongs to the project."""
        path = self._write(tmp_path, PROJECT_ALL_ATTACHED)
        deployed: list[str] = []

        def fake_deploy(self, **kwargs):
            deployed.append(self.name)
            self.id = "project-id"
            return self

        monkeypatch.setattr(rb.Project, "deploy", fake_deploy)

        rows = deploy_file(path)

        assert deployed == ["acme"]
        assert rows and rows[0][0] == "project"


class TestWorkflowSecrets:
    """Workflows can carry secrets directly, without a function to hold them.

    Schedules exist only on workflows, so before this a scheduled job needing
    a credential had to be split into a workflow that dispatches to a
    function purely to borrow its secret mounting.
    """

    def test_secrets_and_env_reach_the_workflow(self) -> None:
        project = rb.project("acme")

        @project.workflow(name="nightly", secrets=["acme-creds"], env={"REGION": "eu"})
        def nightly() -> dict:
            return {}

        assert nightly.secrets == ["acme-creds"]
        assert nightly.env == {"REGION": "eu"}

    def test_defaults_stay_empty(self) -> None:
        project = rb.project("acme")

        @project.workflow(name="nightly")
        def nightly() -> dict:
            return {}

        assert nightly.env == {}
        assert not nightly.secrets

    def test_deploy_sends_resolved_secrets(self, monkeypatch) -> None:
        """The client resolves bundle names to per-key refs before registering."""
        project = rb.project("acme")

        @project.workflow(name="nightly", secrets={"TOKEN": "acme-creds:TOKEN"}, env={"REGION": "eu"})
        def nightly() -> dict:
            return {}

        sent: dict = {}

        def fake_find_workflow(name, project=None):
            return None

        def fake_register_workflow(**kwargs):
            sent.update(kwargs)
            return {"id": "workflow-id", "name": kwargs.get("name")}

        monkeypatch.setattr(nightly._client, "find_workflow", fake_find_workflow)
        monkeypatch.setattr(nightly._client, "register_workflow", fake_register_workflow)
        monkeypatch.setattr(nightly._client, "ensure_project", lambda *a, **k: {"id": "project-id"})

        nightly.deploy()

        assert sent["env"] == {"REGION": "eu"}
        assert sent["secrets"] == {"TOKEN": "acme-creds:TOKEN"}
