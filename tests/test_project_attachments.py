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


PROJECT_WITH_ORPHAN = '''
import rebase as rb

project = rb.project("acme")


@rb.function(project="acme", name="fetch", secrets=["acme-creds"])
def fetch() -> dict:
    return {}


@project.workflow(name="nightly")
def nightly() -> dict:
    return fetch()
'''

PROJECT_ALL_ATTACHED = '''
import rebase as rb

project = rb.project("acme")


@project.function(name="fetch")
def fetch() -> dict:
    return {}
'''


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
