from typing import Any

import pytest

import rebase as rb
from rebase.client import (
    AgentHandle,
    Client,
    Function,
    Model,
    OptimizerHandle,
    Predictor,
    PredictorHandle,
    Project,
    Step,
    Workflow,
)


def test_version_is_exported() -> None:
    assert isinstance(rb.__version__, str)
    assert rb.__version__


def test_huggingface_publish_config_is_exported() -> None:
    config = rb.HuggingFacePublishConfig("rebase/price-forecast", private=False)

    assert config.repo_id == "rebase/price-forecast"
    assert config.private is False
    assert config.repo_type == "model"
    assert config.sync_source_git is True


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


def test_model_base_creates_named_model_without_operation() -> None:
    class BaseEnergyModel(rb.Model):
        name = "price-forecast"

    model = BaseEnergyModel()

    assert isinstance(model, Model)
    assert model.name == "price-forecast"
    assert model.project == "default"
    with pytest.raises(rb.RebaseWorkflowError, match="rebase.Model is not directly deployable"):
        model.deploy()


def test_predictor_base_creates_named_predictor() -> None:
    class PriceForecastPredictor(rb.Predictor):
        name = "price-forecast"

        def predict(self, zone: str = "SE3") -> dict:
            return {"zone": zone}

    predictor = PriceForecastPredictor()

    assert isinstance(predictor, Predictor)
    assert predictor.name == "price-forecast"
    assert predictor.project == "default"
    assert predictor.predict(zone="SE4") == {"zone": "SE4"}


def test_deploy_helper_deploys_targets(monkeypatch) -> None:
    deployed: list[str] = []

    def fake_project_deploy(self: Project, *, replace: bool = False) -> Project:
        deployed.append(self.name)
        return self

    monkeypatch.setattr(Project, "deploy", fake_project_deploy)
    project = rb.project("energy-forecasting")

    assert rb.deploy(project) is project
    assert deployed == ["energy-forecasting"]


def test_deploy_helper_deploys_models(monkeypatch) -> None:
    deployed: list[str] = []

    class PriceForecastPredictor(rb.Predictor):
        name = "price-forecast"

        def predict(self, zone: str = "SE3") -> dict:
            return {"zone": zone}

    def fake_model_deploy(self: Model, *, replace: bool = False) -> Model:
        deployed.append(str(self.name))
        self.id = "function-id"
        return self

    monkeypatch.setattr(Model, "deploy", fake_model_deploy)
    model = PriceForecastPredictor()

    assert rb.deploy(model) is model
    assert deployed == ["price-forecast"]


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


def test_get_model_returns_predict_handle(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_find_model(self: Client, name: str, *, project: str) -> dict[str, Any]:
        observed["project"] = project
        observed["name"] = name
        return {"id": "model-id", "name": name, "operation_name": "predict"}

    monkeypatch.setattr(Client, "find_model", fake_find_model)

    handle = rb.get_model("models/price-forecast")

    assert isinstance(handle, PredictorHandle)
    assert handle.name == "price-forecast"
    assert observed == {"project": "models", "name": "price-forecast"}


def test_typed_model_getters_return_typed_handles(monkeypatch) -> None:
    observed: list[tuple[str, str]] = []

    def fake_find_model(self: Client, name: str, *, project: str) -> dict[str, Any]:
        observed.append((project, name))
        return {"id": f"{name}-id", "name": name, "operation_name": "predict"}

    monkeypatch.setattr(Client, "find_model", fake_find_model)

    assert isinstance(rb.get_predictor("models/price"), PredictorHandle)
    assert isinstance(rb.get_optimizer("models/dispatch"), OptimizerHandle)
    assert isinstance(rb.get_agent("models/controller"), AgentHandle)
    assert observed == [
        ("models", "price"),
        ("models", "dispatch"),
        ("models", "controller"),
    ]


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
