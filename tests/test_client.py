from typing import Any

import pytest

import rebase as rb
from rebase.config import DEFAULT_API_URL, DEFAULT_SERVER_URL, write_profile


class FakeResponse:
    def __init__(self, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any] | list[dict[str, Any]]:
        return self._payload


def test_client_sends_bearer_token(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["headers"] = kwargs["headers"]
        return FakeResponse([])

    monkeypatch.setattr("requests.request", fake_request)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    assert client.list_workflows() == []

    assert observed == {
        "method": "GET",
        "url": "https://workflows.example.com/workflows",
        "headers": {"Authorization": "Bearer rbw_test"},
    }


def test_client_get_project_requests_project_endpoint(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["headers"] = kwargs["headers"]
        return FakeResponse({"id": "project-id", "name": "energy"})

    monkeypatch.setattr("requests.request", fake_request)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    assert client.get_project("project-id") == {"id": "project-id", "name": "energy"}

    assert observed == {
        "method": "GET",
        "url": "https://workflows.example.com/projects/project-id",
        "headers": {"Authorization": "Bearer rbw_test"},
    }


def test_client_lists_run_events_from_events_endpoint(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["headers"] = kwargs["headers"]
        return FakeResponse([{"id": "event-id", "message": "Accepted run request."}])

    monkeypatch.setattr("requests.request", fake_request)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    assert client.list_run_events("run-id") == [{"id": "event-id", "message": "Accepted run request."}]

    assert observed == {
        "method": "GET",
        "url": "https://workflows.example.com/runs/run-id/events",
        "headers": {"Authorization": "Bearer rbw_test"},
    }


def test_client_lists_runs_with_filters(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["headers"] = kwargs["headers"]
        observed["params"] = kwargs["params"]
        return FakeResponse([{"id": "run-id", "status": "succeeded"}])

    monkeypatch.setattr("requests.request", fake_request)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    assert client.list_runs(project_id="project-id", target_type="workflow", limit=25) == [
        {"id": "run-id", "status": "succeeded"}
    ]

    assert observed == {
        "method": "GET",
        "url": "https://workflows.example.com/runs",
        "headers": {"Authorization": "Bearer rbw_test"},
        "params": {
            "project_id": "project-id",
            "target_type": "workflow",
            "limit": 25,
        },
    }


def test_client_uses_hosted_api_url_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(tmp_path / "missing-config.json"))

    client = rb.Client()

    assert client.api_key is None
    assert client.api_url == DEFAULT_API_URL
    assert client.api_url == DEFAULT_SERVER_URL
    assert rb.DEFAULT_SERVER_URL == DEFAULT_SERVER_URL


def test_client_reads_api_key_from_local_profile(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    write_profile(api_key="rbw_profile", profile="default", path=config_path)

    client = rb.Client()

    assert client.api_key == "rbw_profile"
    assert client.api_url == DEFAULT_API_URL


def test_client_reads_api_url_from_local_profile(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    write_profile(api_key="rbw_profile", api_url="http://127.0.0.1:8080", profile="default", path=config_path)

    client = rb.Client()

    assert client.api_key == "rbw_profile"
    assert client.api_url == "http://127.0.0.1:8080"


def test_client_can_select_named_profile(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    write_profile(api_key="rbw_default", profile="default", path=config_path)
    write_profile(api_key="rbw_prod", profile="prod", path=config_path)

    client = rb.Client(profile="prod")

    assert client.api_key == "rbw_prod"
    assert client.api_url == DEFAULT_API_URL


def test_list_functions_with_project_name_does_not_create_project(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        return FakeResponse([])

    monkeypatch.setattr("requests.request", fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "find_project", lambda name: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: (_ for _ in ()).throw(AssertionError()))

    assert client.list_functions(project="energy") == []
    assert observed["method"] == "GET"
    assert observed["url"] == "https://workflows.example.com/projects/project-id/functions"


def test_workflow_deploy_updates_existing_workflow_version(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(
        client,
        "list_workflows",
        lambda: [
            {
                "id": "workflow-id",
                "name": "forecast",
                "flow_ref": "app.flows.energy:forecast",
            }
        ],
    )
    observed: dict[str, Any] = {}

    def fake_update_workflow(workflow_id: str, **kwargs: Any) -> dict[str, Any]:
        observed["workflow_id"] = workflow_id
        observed.update(kwargs)
        return {"id": workflow_id, "name": "forecast", "current_version_id": "version-id"}

    monkeypatch.setattr(client, "update_workflow", fake_update_workflow)

    def forecast(site_id: str) -> dict:
        return {"site_id": site_id}

    workflow = rb.Workflow(forecast, client=client).deploy()

    assert workflow.id == "workflow-id"
    assert observed["workflow_id"] == "workflow-id"
    assert observed["entrypoint"] == "forecast"
    assert observed["source_code"].startswith("def forecast")
    assert observed["step_graph"] is None
    assert observed["execution_backend"] == rb.DEFAULT_WORKFLOW_BACKEND


def test_update_workflow_omits_step_graph_unless_explicit(monkeypatch) -> None:
    observed_payloads: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed_payloads.append(kwargs["json"])
        return FakeResponse({"id": "workflow-id", "name": "forecast"})

    monkeypatch.setattr("requests.request", fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    client.update_workflow("workflow-id", description="Updated")
    client.update_workflow("workflow-id", step_graph=None)
    client.update_workflow("workflow-id", schedule=None)

    assert observed_payloads[0] == {"description": "Updated"}
    assert observed_payloads[1] == {"step_graph": None}
    assert observed_payloads[2] == {"schedule": None}


def test_workflow_deploy_registers_function_source(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "list_workflows", lambda: [])

    def fake_register_workflow(**kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return {"id": "workflow-id"}

    monkeypatch.setattr(client, "register_workflow", fake_register_workflow)

    def add(left: float = 0, right: float = 0) -> dict:
        return {"sum": left + right}

    workflow = rb.Workflow(add, name="add-numbers", client=client).deploy()

    assert workflow.id == "workflow-id"
    assert observed["name"] == "add-numbers"
    assert observed["flow_ref"] is None
    assert observed["entrypoint"] == "add"
    assert "def add(left: float = 0, right: float = 0) -> dict:" in observed["source_code"]
    assert observed["default_parameters"] == {"left": 0, "right": 0}
    assert observed["execution_backend"] == rb.DEFAULT_WORKFLOW_BACKEND
    assert workflow.execution_backend == rb.DEFAULT_WORKFLOW_BACKEND


def test_workflow_can_use_prefect_cloud_run_jobs_backend(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "list_workflows", lambda: [])
    monkeypatch.setattr(client, "register_workflow", lambda **kwargs: observed.update(kwargs) or {"id": "workflow-id"})

    def forecast(site_id: str = "site-001") -> dict:
        return {"site_id": site_id}

    workflow = rb.Workflow(
        forecast,
        name="cloud-run-forecast",
        backend="prefect_cloud_run_jobs",
        client=client,
    ).deploy()

    assert workflow.execution_backend == "prefect_cloud_run_jobs"
    assert observed["execution_backend"] == "prefect_cloud_run_jobs"


def test_workflow_rejects_unknown_backend() -> None:
    def forecast() -> dict:
        return {"status": "ok"}

    with pytest.raises(ValueError, match="workflow backend"):
        rb.Workflow(forecast, project="energy", backend="unknown")


def test_workflow_explicit_defaults_override_function_defaults(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "list_workflows", lambda: [])
    monkeypatch.setattr(client, "register_workflow", lambda **kwargs: observed.update(kwargs) or {"id": "workflow-id"})

    def forecast(site_id: str, horizon_hours: int = 24) -> dict:
        return {"site_id": site_id, "horizon_hours": horizon_hours}

    rb.Workflow(forecast, default_parameters={"horizon_hours": 48}, client=client).deploy()

    assert observed["default_parameters"] == {"horizon_hours": 48}


def test_project_deploy_registers_function_source(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)

    def fake_register_function(**kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return {"id": "function-id", "name": kwargs["name"]}

    monkeypatch.setattr(client, "register_function", fake_register_function)

    project = rb.Project("energy-forecasting", client=client)

    @project.function(name="normalize-weather")
    def normalize_weather(site_id: str, horizon_hours: int = 24) -> dict:
        return {"site_id": site_id, "horizon_hours": horizon_hours}

    project.deploy()

    assert normalize_weather.id == "function-id"
    assert observed["project"] == "energy-forecasting"
    assert observed["name"] == "normalize-weather"
    assert observed["entrypoint"] == "normalize_weather"
    assert observed["source_code"].startswith("def normalize_weather")
    assert "@project.function" not in observed["source_code"]
    assert "def normalize_weather(site_id: str, horizon_hours: int = 24) -> dict:" in observed["source_code"]
    assert observed["default_parameters"] == {"horizon_hours": 24}
    assert observed["execution_backend"] == "cloud_run"
    assert normalize_weather.execution_backend == rb.DEFAULT_FUNCTION_BACKEND


def test_project_function_can_use_prefect_backend(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "register_function", lambda **kwargs: observed.update(kwargs) or {"id": "function-id"})

    project = rb.Project("energy-forecasting", client=client)

    @project.function(name="legacy-function", backend="prefect")
    def legacy_function() -> dict:
        return {"status": "ok"}

    project.deploy()

    assert legacy_function.execution_backend == "prefect"
    assert observed["execution_backend"] == "prefect"


def test_project_function_can_use_prefect_cloud_backend(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "register_function", lambda **kwargs: observed.update(kwargs) or {"id": "function-id"})

    project = rb.Project("energy-forecasting", client=client)

    @project.function(name="prefect-cloud-function", backend="prefect_cloud")
    def prefect_cloud_function() -> dict:
        return {"status": "ok"}

    project.deploy()

    assert prefect_cloud_function.execution_backend == "prefect_cloud"
    assert observed["execution_backend"] == "prefect_cloud"


def test_project_function_can_use_cloud_run_backend(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "register_function", lambda **kwargs: observed.update(kwargs) or {"id": "function-id"})

    project = rb.Project("energy-forecasting", client=client)

    @project.function(name="cloud-run-function", backend="cloud_run")
    def cloud_run_function() -> dict:
        return {"status": "ok"}

    project.deploy()

    assert cloud_run_function.execution_backend == "cloud_run"
    assert observed["execution_backend"] == "cloud_run"


def test_project_function_can_use_cloud_run_shared_backend(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "register_function", lambda **kwargs: observed.update(kwargs) or {"id": "function-id"})

    project = rb.Project("energy-forecasting", client=client)

    @project.function(name="cloud-run-shared-function", backend="cloud_run_shared")
    def cloud_run_shared_function() -> dict:
        return {"status": "ok"}

    project.deploy()

    assert cloud_run_shared_function.execution_backend == "cloud_run_shared"
    assert observed["execution_backend"] == "cloud_run_shared"


def test_project_function_sends_cloud_run_isolation_settings(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "register_function", lambda **kwargs: observed.update(kwargs) or {"id": "function-id"})

    project = rb.Project("energy-forecasting", client=client)

    @project.function(name="cloud-run-function", backend="cloud_run", min_instances=1, concurrency=1)
    def cloud_run_function() -> dict:
        return {"status": "ok"}

    project.deploy()

    assert observed["cloud_run_min_instances"] == 1
    assert observed["cloud_run_concurrency"] == 1


def test_project_function_rejects_unknown_backend() -> None:
    project = rb.Project("energy-forecasting")

    with pytest.raises(ValueError, match="function backend"):

        @project.function(backend="unknown")
        def bad_backend() -> dict:
            return {"status": "ok"}


def test_project_function_serializes_dependencies(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "register_function", lambda **kwargs: observed.update(kwargs) or {"id": "function-id"})

    project = rb.Project("energy-forecasting", client=client)

    @project.function(name="numpy-function", dependencies=["numpy==2.3.0"])
    def numpy_function() -> dict:
        return {"status": "ok"}

    project.deploy()

    assert observed["image_spec"] == {
        "kind": "python",
        "python_version": "3.13",
        "uv_pip_packages": ["numpy==2.3.0"],
        "uv_version": None,
    }


def test_function_deploy_sends_default_image_spec_when_dependencies_removed(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(
        client,
        "find_function",
        lambda name, *, project: {"id": "function-id", "name": name, "project": project},
    )

    def fake_update_function(function_id: str, **kwargs: Any) -> dict[str, Any]:
        observed["function_id"] = function_id
        observed.update(kwargs)
        return {"id": function_id, "name": "add"}

    monkeypatch.setattr(client, "update_function", fake_update_function)

    def add(a: int = 0, b: int = 0) -> dict[str, int]:
        return {"sum": a + b}

    rb.Function(add, project="math", backend="cloud_run", client=client).deploy()

    assert observed["image_spec"] == {
        "kind": "python",
        "python_version": "3.13",
        "uv_pip_packages": [],
        "uv_version": None,
    }


def test_project_function_serializes_image_builder(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "register_function", lambda **kwargs: observed.update(kwargs) or {"id": "function-id"})

    image = rb.Image.python("3.13").uv_pip_install("pandas==2.3.0", uv_version="0.9.0")
    project = rb.Project("energy-forecasting", client=client)

    @project.function(name="pandas-function", image=image)
    def pandas_function() -> dict:
        return {"status": "ok"}

    project.deploy()

    assert observed["image_spec"] == {
        "kind": "python",
        "python_version": "3.13",
        "uv_pip_packages": ["pandas==2.3.0"],
        "uv_version": "0.9.0",
    }


def test_project_deploy_registers_step_workflow_graph(monkeypatch) -> None:
    observed_functions: list[dict[str, Any]] = []
    observed_workflow: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "find_workflow", lambda name, *, project=None: None)

    def fake_register_function(**kwargs: Any) -> dict[str, Any]:
        observed_functions.append(kwargs)
        return {
            "id": f"{kwargs['name']}-id",
            "name": kwargs["name"],
            "current_version_id": f"{kwargs['name']}-version-id",
        }

    def fake_register_workflow(**kwargs: Any) -> dict[str, Any]:
        observed_workflow.update(kwargs)
        return {"id": "workflow-id", "name": kwargs["name"], "current_version_id": "workflow-version-id"}

    monkeypatch.setattr(client, "register_function", fake_register_function)
    monkeypatch.setattr(client, "register_workflow", fake_register_workflow)

    project = rb.Project("energy-forecasting", client=client)

    @project.step(name="load-weather", retries=2, timeout_seconds=30)
    def load_weather(site_id: str) -> dict:
        return {"site_id": site_id}

    @project.step(name="build-forecast")
    def build_forecast(weather: dict, horizon_hours: int = 24) -> dict:
        return {"weather": weather, "horizon_hours": horizon_hours}

    @project.workflow(name="site-forecast")
    def site_forecast(site_id: str, horizon_hours: int = 24) -> dict:
        weather = load_weather(site_id)
        forecast = build_forecast(weather, horizon_hours=horizon_hours)
        return {"forecast": forecast}

    project.deploy()

    assert [item["name"] for item in observed_functions] == ["load-weather", "build-forecast"]
    assert [item["execution_backend"] for item in observed_functions] == ["prefect", "prefect"]
    assert observed_workflow["execution_backend"] == rb.DEFAULT_WORKFLOW_BACKEND
    graph = observed_workflow["step_graph"]
    assert graph["schema_version"] == 1
    assert graph["engine"] == "prefect"
    assert graph["return_binding"] == {
        "type": "dict",
        "items": {"forecast": {"type": "node_output", "node_key": "build_forecast"}},
    }

    load_node, build_node = graph["nodes"]
    assert load_node["node_key"] == "load_weather"
    assert load_node["name"] == "load-weather"
    assert load_node["function_id"] == "load-weather-id"
    assert load_node["function_version_id"] == "load-weather-version-id"
    assert load_node["input_bindings"] == {"site_id": {"type": "parameter", "name": "site_id"}}
    assert load_node["retry_policy"] == {"retries": 2}
    assert load_node["timeout_seconds"] == 30

    assert build_node["node_key"] == "build_forecast"
    assert build_node["function_version_id"] == "build-forecast-version-id"
    assert build_node["upstream_node_keys"] == ["load_weather"]
    assert build_node["input_bindings"] == {
        "weather": {"type": "node_output", "node_key": "load_weather"},
        "horizon_hours": {"type": "parameter", "name": "horizon_hours"},
    }


def test_project_workflow_deploy_sends_cron_schedule(monkeypatch) -> None:
    observed_workflow: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_workflow", lambda name, *, project=None: None)
    monkeypatch.setattr(client, "register_workflow", lambda **kwargs: observed_workflow.update(kwargs) or {"id": "id"})

    project = rb.Project("energy-forecasting", client=client)

    @project.workflow(
        name="scheduled-forecast",
        schedule=rb.Cron("0 6 * * *", timezone="Europe/Stockholm"),
    )
    def scheduled_forecast(site_id: str = "site-001", zone: str = "SE3") -> dict:
        return {"site_id": site_id, "zone": zone}

    project.deploy()

    assert scheduled_forecast.schedule == {
        "type": "cron",
        "cron": "0 6 * * *",
        "timezone": "Europe/Stockholm",
        "day_or": True,
        "active": True,
    }
    assert observed_workflow["schedule"] == scheduled_forecast.schedule
    assert observed_workflow["default_parameters"] == {"site_id": "site-001", "zone": "SE3"}
    assert observed_workflow["required_parameters"] == []


def test_scheduled_workflow_requires_defaults(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        raise AssertionError("scheduled workflow validation should run before API calls")

    monkeypatch.setattr("requests.request", fake_request)

    project = rb.Project("energy-forecasting", client=client)

    @project.workflow(
        name="scheduled-forecast",
        schedule=rb.Cron("0 6 * * *", timezone="Europe/Stockholm"),
    )
    def scheduled_forecast(site_id: str, zone: str = "SE3") -> dict:
        return {"site_id": site_id, "zone": zone}

    with pytest.raises(rb.RebaseWorkflowError, match="Missing defaults: site_id"):
        project.deploy()


def test_project_deploy_sends_source_settings(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    def fake_ensure_project(name: str, **kwargs: Any) -> dict[str, Any]:
        observed["name"] = name
        observed.update(kwargs)
        return {"id": "project-id", "name": name}

    monkeypatch.setattr(client, "ensure_project", fake_ensure_project)

    project = rb.Project(
        "energy-forecasting",
        source_mode="workspace_repo",
        repo_path="projects/energy-forecasting",
        client=client,
    )
    project.deploy()

    assert observed == {
        "name": "energy-forecasting",
        "description": None,
        "source_mode": "workspace_repo",
        "repo_owner": None,
        "repo_name": None,
        "repo_path": "projects/energy-forecasting",
    }


def test_function_from_name_spawns_remote_run(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(
        client,
        "find_function",
        lambda name, *, project: {"id": "function-id", "name": name, "project": project},
    )

    observed: dict[str, Any] = {}

    def fake_run_function(function_id: str, parameters: dict[str, Any] | None = None) -> rb.Run:
        observed["function_id"] = function_id
        observed["parameters"] = parameters
        return rb.Run("run-id", client=client, data={"id": "run-id", "status": "submitted"})

    monkeypatch.setattr(client, "run_function", fake_run_function)

    function = rb.Function.from_name("shared-energy-utils", "normalize-weather", client=client)
    run = function.spawn(site_id="site-001")

    assert run.id == "run-id"
    assert observed == {
        "function_id": "function-id",
        "parameters": {"site_id": "site-001"},
    }


def test_run_lists_step_runs(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(
        client,
        "list_run_steps",
        lambda run_id: [{"id": "step-run-id", "workflow_run_id": run_id, "node_key": "load_weather"}],
    )

    run = rb.Run("run-id", client=client)

    assert run.steps() == [{"id": "step-run-id", "workflow_run_id": "run-id", "node_key": "load_weather"}]


def test_decorated_targets_call_local_python_without_cloud(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "run_ephemeral", lambda **kwargs: pytest.fail("local calls should not use cloud"))
    project = rb.Project("hello", client=client)

    @project.function()
    def add(a: int, b: int) -> int:
        return a + b

    @project.step()
    def load_name(name: str) -> dict:
        return {"name": name}

    @project.step()
    def package(payload: dict) -> dict:
        return {"message": f"Hello, {payload['name']}!"}

    @project.workflow()
    def hello(name: str) -> dict:
        return package(load_name(name))

    assert add(1, 2) == 3
    assert load_name("Rebase") == {"name": "Rebase"}
    assert hello("Rebase") == {"message": "Hello, Rebase!"}


def test_function_ephemeral_run_sends_source_without_deploy(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    observed: dict[str, Any] = {}

    def fake_run_ephemeral(**kwargs: Any) -> rb.Run:
        observed.update(kwargs)
        return rb.Run("run-id", client=client, data={"id": "run-id", "status": "submitted"})

    monkeypatch.setattr(client, "run_ephemeral", fake_run_ephemeral)
    monkeypatch.setattr(rb.Function, "deploy", lambda self, **kwargs: pytest.fail("ephemeral run should not deploy"))

    def add(a: int, b: int) -> dict:
        return {"sum": a + b}

    function = rb.Function(add, project="math", name="add", client=client)
    run = function.ephemeral_run(a=1, b=2)

    assert run.id == "run-id"
    assert observed["target_type"] == "function"
    assert observed["project"] == "math"
    assert observed["name"] == "add"
    assert observed["entrypoint"] == "add"
    assert observed["parameters"] == {"a": 1, "b": 2}
    assert observed["execution_backend"] == "cloud_run"
    assert "def add(a: int, b: int) -> dict:" in observed["source_code"]


def test_predictor_as_function_generates_predict_wrapper() -> None:
    class PriceForecastPredictor(rb.Predictor):
        name = "price-forecast"

        def predict(self, zone: str = "SE3", horizon_hours: int = 24) -> dict:
            return {"zone": zone, "horizon_hours": horizon_hours}

    model = PriceForecastPredictor(project="models", dependencies=["boltons==25.0.0"])
    function = model.as_function()

    assert function.project == "models"
    assert function.name == "price-forecast"
    assert function.entrypoint == "predict"
    assert function.default_parameters == {"zone": "SE3", "horizon_hours": 24}
    assert function.execution_backend == "cloud_run"
    assert function.image_spec is not None
    assert "boltons==25.0.0" in function.image_spec["uv_pip_packages"]
    assert any("emflow" in package for package in function.image_spec["uv_pip_packages"])
    assert "class PriceForecastPredictor(rb.Predictor):" in str(function.source_code)
    assert "def predict(zone: str = 'SE3', horizon_hours: int = 24) -> dict:" in str(function.source_code)
    assert "model = PriceForecastPredictor()" in str(function.source_code)
    assert "return model.predict(zone=zone, horizon_hours=horizon_hours)" in str(function.source_code)


def test_predictor_deploy_registers_model(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "find_model", lambda name, *, project: None)

    def fake_register_model(**kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return {"id": "model-id", "name": kwargs["name"], "current_version_id": "version-id"}

    monkeypatch.setattr(client, "register_model", fake_register_model)

    class PriceForecastPredictor(rb.Predictor):
        name = "price-forecast"

        def predict(self, zone: str = "SE3") -> dict:
            return {"zone": zone}

    model = PriceForecastPredictor(project="models", client=client).deploy()

    assert model.id == "model-id"
    assert observed["project"] == "models"
    assert observed["name"] == "price-forecast"
    assert observed["kind"] == "predictor"
    assert observed["operation_name"] == "predict"
    assert observed["environment"] == "dev"
    assert observed["default_parameters"] == {"zone": "SE3"}
    assert observed["execution_backend"] == "cloud_run"
    assert any("emflow" in package for package in observed["image_spec"]["uv_pip_packages"])


def test_predictor_ephemeral_run_sends_model_payload(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    observed: dict[str, Any] = {}

    def fake_run_ephemeral(**kwargs: Any) -> rb.Run:
        observed.update(kwargs)
        return rb.Run("run-id", client=client, data={"id": "run-id", "status": "submitted"})

    monkeypatch.setattr(client, "run_ephemeral", fake_run_ephemeral)
    monkeypatch.setattr(
        rb.Function,
        "deploy",
        lambda self, **kwargs: pytest.fail("ephemeral model run should not deploy"),
    )

    class PriceForecastPredictor(rb.Predictor):
        name = "price-forecast"

        def predict(self, zone: str = "SE3") -> dict:
            return {"zone": zone}

    run = PriceForecastPredictor(project="models", client=client).ephemeral_run(zone="SE4")

    assert run.id == "run-id"
    assert observed["target_type"] == "model"
    assert observed["project"] == "models"
    assert observed["name"] == "price-forecast"
    assert observed["entrypoint"] == "predict"
    assert observed["parameters"] == {"zone": "SE4"}
    assert observed["default_parameters"] == {"zone": "SE3"}
    assert observed["execution_backend"] == "cloud_run"


def test_predictor_handle_runs_model(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(
        client,
        "find_model",
        lambda name, *, project: {
            "id": "model-id",
            "name": name,
            "operation_name": "predict",
            "project_id": "project-id",
        },
    )
    observed: dict[str, Any] = {}

    def fake_run_model(model_id: str, parameters: dict[str, Any] | None = None, *, environment: str = "dev") -> rb.Run:
        observed["model_id"] = model_id
        observed["parameters"] = parameters
        observed["environment"] = environment
        return rb.Run(
            "run-id",
            client=client,
            data={"id": "run-id", "status": "succeeded", "result": {"zone": "SE4"}},
        )

    monkeypatch.setattr(client, "run_model", fake_run_model)
    monkeypatch.setattr(
        client,
        "get_run",
        lambda run_id: {"id": run_id, "status": "succeeded", "result": {"zone": "SE4"}},
    )

    result = rb.Predictor.from_name("models", "price-forecast", client=client).predict.remote(
        environment="staging",
        zone="SE4",
    )

    assert result == {"zone": "SE4"}
    assert observed == {
        "model_id": "model-id",
        "parameters": {"zone": "SE4"},
        "environment": "staging",
    }


def test_optimizer_as_function_generates_optimize_wrapper() -> None:
    class DispatchOptimizer(rb.Optimizer):
        name = "dispatch-optimizer"

        def optimize(self, site_id: str, horizon_hours: int = 24) -> dict:
            return {"site_id": site_id, "horizon_hours": horizon_hours}

    function = DispatchOptimizer(project="models").as_function()

    assert function.name == "dispatch-optimizer"
    assert function.entrypoint == "optimize"
    assert function.default_parameters == {"horizon_hours": 24}
    assert "class DispatchOptimizer(rb.Optimizer):" in str(function.source_code)
    assert "def optimize(site_id: str, horizon_hours: int = 24) -> dict:" in str(function.source_code)
    assert "return model.optimize(site_id=site_id, horizon_hours=horizon_hours)" in str(function.source_code)


def test_agent_as_function_generates_act_wrapper() -> None:
    class BatteryAgent(rb.Agent):
        name = "battery-agent"

        def act(self, state: dict) -> dict:
            return {"action": "hold", "state": state}

    function = BatteryAgent(project="models").as_function()

    assert function.name == "battery-agent"
    assert function.entrypoint == "act"
    assert function.default_parameters == {}
    assert "class BatteryAgent(rb.Agent):" in str(function.source_code)
    assert "def act(state: dict) -> dict:" in str(function.source_code)
    assert "return model.act(state=state)" in str(function.source_code)


def test_plain_model_is_not_directly_deployable() -> None:
    class BaseEnergyModel(rb.Model):
        name = "base-energy-model"

    with pytest.raises(rb.RebaseWorkflowError, match="rebase.Model is not directly deployable"):
        BaseEnergyModel().as_function()


def test_simulator_is_local_only_for_cloud_deploy() -> None:
    class BatterySimulator(rb.Simulator):
        name = "battery-simulator"

        def _transition_function(self, state, action):
            return state

        def _gather_info(self):
            return {}

        def reset(self):
            return {}

        def step(self, action=None):
            return {}, {}

    with pytest.raises(rb.RebaseWorkflowError, match="Simulator cloud deployment is not supported"):
        BatterySimulator().deploy()


def test_workflow_ephemeral_run_embeds_step_sources_without_deploy(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    observed: dict[str, Any] = {}

    def fake_run_ephemeral(**kwargs: Any) -> rb.Run:
        observed.update(kwargs)
        return rb.Run("run-id", client=client, data={"id": "run-id", "status": "submitted"})

    monkeypatch.setattr(client, "run_ephemeral", fake_run_ephemeral)
    monkeypatch.setattr(rb.Workflow, "deploy", lambda self, **kwargs: pytest.fail("ephemeral run should not deploy"))
    project = rb.Project("hello", client=client)

    @project.step()
    def load_name(name: str = "World") -> dict:
        return {"name": name}

    @project.step()
    def package(payload: dict) -> dict:
        return {"message": f"Hello, {payload['name']}!"}

    @project.workflow(name="hello-workflow")
    def hello(name: str = "World") -> dict:
        return package(load_name(name))

    run = hello.ephemeral_run(name="Rebase")

    assert run.id == "run-id"
    assert observed["target_type"] == "workflow"
    assert observed["project"] == "hello"
    assert observed["name"] == "hello-workflow"
    assert observed["parameters"] == {"name": "Rebase"}
    assert observed["execution_backend"] == rb.DEFAULT_WORKFLOW_BACKEND
    nodes = observed["step_graph"]["nodes"]
    assert [node["name"] for node in nodes] == ["load-name", "package"]
    assert all(node["function_version_id"] is None for node in nodes)
    assert all("source_code" in node and node["source_code"] for node in nodes)
