from typing import Any

import rebase as rb
from rebase.client import Client, Function, Project, Step, Workflow


def test_version_is_exported() -> None:
    assert isinstance(rb.__version__, str)
    assert rb.__version__


def test_project_helper_returns_project() -> None:
    project = rb.project("energy-forecasting", description="Forecasts")

    assert isinstance(project, Project)
    assert project.name == "energy-forecasting"
    assert project.description == "Forecasts"


def test_function_helper_creates_function_handle() -> None:
    @rb.function(project="energy-tools", name="add")
    def add(a: int = 0, b: int = 0) -> dict:
        return {"sum": a + b}

    assert isinstance(add, Function)
    assert add.project == "energy-tools"
    assert add.name == "add"


def test_function_helper_defaults_project() -> None:
    @rb.function(name="add")
    def add(a: int = 0, b: int = 0) -> dict:
        return {"sum": a + b}

    assert isinstance(add, Function)
    assert add.project == "default"
    assert add.name == "add"


def test_step_helper_defaults_project() -> None:
    @rb.step(name="load-name")
    def load_name(name: str = "World") -> dict:
        return {"name": name}

    assert isinstance(load_name, Step)
    assert load_name.project == "default"
    assert load_name.name == "load-name"


def test_workflow_helper_creates_workflow_handle() -> None:
    @rb.workflow(project="forecasting")
    def forecast(site_id: str = "site-001") -> dict:
        return {"site_id": site_id}

    assert isinstance(forecast, Workflow)
    assert forecast.project == "forecasting"
    assert forecast.name == "forecast"


def test_workflow_helper_defaults_project() -> None:
    @rb.workflow(name="hello-workflow")
    def hello_workflow(name: str = "World") -> dict:
        return {"message": f"Hello, {name}!"}

    assert isinstance(hello_workflow, Workflow)
    assert hello_workflow.project == "default"
    assert hello_workflow.name == "hello-workflow"


def test_deploy_helper_deploys_targets(monkeypatch) -> None:
    deployed: list[str] = []

    def fake_project_deploy(self: Project, *, replace: bool = False) -> Project:
        deployed.append(self.name)
        return self

    monkeypatch.setattr(Project, "deploy", fake_project_deploy)
    project = rb.project("energy-forecasting")

    assert rb.deploy(project) is project
    assert deployed == ["energy-forecasting"]


def test_get_function_resolves_project_name(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_from_name(project: str, name: str) -> Function:
        observed["project"] = project
        observed["name"] = name
        return Function(project=project, name=name, function_id="function-id")

    monkeypatch.setattr(Function, "from_name", staticmethod(fake_from_name))

    handle = rb.get_function("shared-utils/normalize-weather")

    assert handle.name == "normalize-weather"
    assert observed == {"project": "shared-utils", "name": "normalize-weather"}


def test_workspace_helper_uses_default_client(monkeypatch) -> None:
    monkeypatch.setattr(Client, "get_workspace", lambda self: {"id": "workspace-id", "name": "ACME"})

    assert rb.workspace() == {"id": "workspace-id", "name": "ACME"}


def test_update_workspace_helper_uses_default_client(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_update_workspace(self: Client, **kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return {"id": "workspace-id", **kwargs}

    monkeypatch.setattr(Client, "update_workspace", fake_update_workspace)

    result = rb.update_workspace(source_mode="workspace_repo", repo_name="platform-workflows")

    assert result["repo_name"] == "platform-workflows"
    assert observed["source_mode"] == "workspace_repo"
